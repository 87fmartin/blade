#!/usr/bin/env python3
"""blade.py — Detect buying signals in a Clay table and emit alert drafts.

Reads sales prospects, fires signals on job changes/promotions and hiring sprees,
and writes a JSON file of per-owner alert drafts plus a digest summary for an
external alerting agent (e.g. OpenClaw) to deliver. Delivery routing — Slack
handles, channels, etc. — lives in the agent, not here. Dedupes via a local
JSON state file so the same signal does not re-fire next run.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build


# === Column names — remap here if your Clay table uses different headers ===
COL_FULL_NAME = "Full Name"
COL_TITLE = "Title"
COL_ORG = "Org"
COL_LINKEDIN = "LinkedIn URL"
COL_JOB_CHANGE = "Promotion or Job Change"
COL_JOB_OPENINGS = "Job Openings"
COL_OWNER_ID = "HubSpot Owner ID"

HIRING_SPREE_MIN = 5
STATE_FILE = Path("blade_state.json")
ALERTS_OUT_FILE = Path("blade_alerts.json")


@dataclass
class Prospect:
    full_name: str
    title: str
    org: str
    linkedin: str
    job_change_raw: str
    job_openings: int
    owner_id: str

    @property
    def signals(self) -> list[str]:
        sigs: list[str] = []
        raw = self.job_change_raw.strip().lower()
        if raw:
            if "promotion" in raw:
                sigs.append("promotion")
            else:
                sigs.append("job_change")
        if self.job_openings >= HIRING_SPREE_MIN:
            sigs.append("hiring_spree")
        return sigs

    @property
    def priority(self) -> int | None:
        sigs = set(self.signals)
        has_change = bool(sigs & {"promotion", "job_change"})
        has_hiring = "hiring_spree" in sigs
        if has_change and has_hiring:
            return 1
        if has_change:
            return 2
        if has_hiring:
            return 3
        return None


def load_csv(path: Path) -> list[Prospect]:
    with path.open(newline="", encoding="utf-8") as f:
        return _parse_rows(list(csv.DictReader(f)))


def load_clay(api_key: str, table_id: str) -> list[Prospect]:
    """Fetch rows from a Clay table.

    Clay's workspace API surface varies; adjust this URL/shape to whatever your
    workspace exposes. The default assumes a JSON response with rows under
    `rows` or `data`.
    """
    url = f"https://api.clay.com/v1/tables/{table_id}/rows"
    r = requests.get(url, headers={"Authorization": f"Bearer {api_key}"}, timeout=60)
    r.raise_for_status()
    payload = r.json()
    rows = payload.get("rows") or payload.get("data") or payload
    if not isinstance(rows, list):
        raise RuntimeError(f"unexpected Clay payload shape: {type(rows)}")
    return _parse_rows(rows)


SHEETS_READONLY_SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"


def load_gsheet(spreadsheet_id: str, range_str: str, creds_path: str) -> list[Prospect]:
    """Fetch rows from a Google Sheet via the Sheets API.

    `creds_path` points to a service-account JSON key. The first row of
    `range_str` is treated as the header row.
    """
    creds = service_account.Credentials.from_service_account_file(
        creds_path, scopes=[SHEETS_READONLY_SCOPE]
    )
    service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    result = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=range_str)
        .execute()
    )
    values = result.get("values") or []
    if len(values) < 2:
        return []
    headers, *rows = values
    dicts = [dict(zip(headers, row)) for row in rows]
    return _parse_rows(dicts)


def _normalize_linkedin(url: str) -> str:
    url = url.strip()
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url.lstrip("/")
    return url


def _parse_rows(rows: Iterable[dict]) -> list[Prospect]:
    out: list[Prospect] = []
    for row in rows:
        openings_raw = row.get(COL_JOB_OPENINGS) or 0
        try:
            openings = int(str(openings_raw).strip() or 0)
        except ValueError:
            openings = 0
        out.append(
            Prospect(
                full_name=str(row.get(COL_FULL_NAME, "")).strip(),
                title=str(row.get(COL_TITLE, "")).strip(),
                org=str(row.get(COL_ORG, "")).strip(),
                linkedin=_normalize_linkedin(str(row.get(COL_LINKEDIN, ""))),
                job_change_raw=str(row.get(COL_JOB_CHANGE, "")).strip(),
                job_openings=openings,
                owner_id=str(row.get(COL_OWNER_ID, "")).strip(),
            )
        )
    return out


def load_state(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open() as f:
        return json.load(f)


def save_state(path: Path, state: dict) -> None:
    with path.open("w") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def state_key(p: Prospect) -> str:
    return p.linkedin or f"{p.full_name}|{p.org}".lower()


def should_alert(p: Prospect, state: dict) -> bool:
    current = sorted(p.signals)
    prev = state.get(state_key(p), {}).get("signals")
    return current != (sorted(prev) if prev else None)


def record_alert(p: Prospect, state: dict) -> None:
    state[state_key(p)] = {
        "signals": sorted(p.signals),
        "priority": p.priority,
        "alerted_at": datetime.now(timezone.utc).isoformat(),
    }


def format_signal_lines(p: Prospect) -> list[str]:
    parts = []
    if "promotion" in p.signals:
        parts.append(f"Promotion — now {p.title} at {p.org}")
    if "job_change" in p.signals:
        parts.append(f"Job change — now {p.title} at {p.org}")
    if "hiring_spree" in p.signals:
        parts.append(f"Hiring spree — {p.job_openings} open AI/engineering roles")
    return parts


def build_alert_text(p: Prospect) -> str:
    lines = [
        f"*Buying signal — Priority {p.priority}*",
        f"*{p.full_name}* — {p.title} @ {p.org}",
        "",
        "Signals:",
    ]
    lines.extend(f"  • {s}" for s in format_signal_lines(p))
    if p.linkedin:
        lines.append("")
        lines.append(f"LinkedIn: {p.linkedin}")
    return "\n".join(lines)


def build_digest(prospects: list[Prospect]) -> str:
    by_priority: dict[int, list[Prospect]] = {1: [], 2: [], 3: []}
    for p in prospects:
        if p.priority:
            by_priority[p.priority].append(p)
    total = sum(len(v) for v in by_priority.values())
    titles = {
        1: "Priority 1 — job change/promotion + hiring spree",
        2: "Priority 2 — job change/promotion",
        3: "Priority 3 — hiring spree",
    }
    lines = [f"*Blade run — {total} signal{'s' if total != 1 else ''} fired*", ""]
    for prio in (1, 2, 3):
        items = by_priority[prio]
        if not items:
            continue
        lines.append(f"*{titles[prio]} ({len(items)})*")
        for p in items:
            sigs = ", ".join(p.signals)
            link = f" — {p.linkedin}" if p.linkedin else ""
            lines.append(f"  • {p.full_name} — {p.title} @ {p.org} [{sigs}]{link}")
        lines.append("")
    return "\n".join(lines).rstrip()


def build_alerts_payload(to_alert: list[Prospect]) -> dict:
    alerts = []
    for p in to_alert:
        alerts.append({
            "owner_id": p.owner_id or None,
            "prospect": p.full_name,
            "priority": p.priority,
            "text": build_alert_text(p),
        })
    payload: dict = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "alerts": alerts,
    }
    if to_alert:
        payload["digest"] = build_digest(to_alert)
    return payload


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Detect buying signals and emit alert drafts as JSON."
    )
    ap.add_argument("--csv", help="Read from a local CSV instead of the Clay API")
    ap.add_argument(
        "--sheet",
        help="Read from a Google Sheet by ID (via gws). Falls back to $CLAY_SHEET_ID.",
    )
    ap.add_argument(
        "--sheet-range",
        default=os.environ.get("CLAY_SHEET_RANGE", "Sheet1"),
        help="A1 range to read from the sheet (default: Sheet1, or $CLAY_SHEET_RANGE)",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Print alerts to stdout; don't write JSON or update state",
    )
    ap.add_argument(
        "--reset-state",
        action="store_true",
        help="Clear dedup state and re-alert everything on this run",
    )
    args = ap.parse_args()

    if args.reset_state and STATE_FILE.exists():
        STATE_FILE.unlink()
        print(f"cleared {STATE_FILE}")

    sheet_id = args.sheet or os.environ.get("CLAY_SHEET_ID")
    if args.csv:
        prospects = load_csv(Path(args.csv))
    elif sheet_id:
        creds_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or os.environ.get(
            "GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE"
        )
        if not creds_path:
            sys.exit(
                "set GOOGLE_APPLICATION_CREDENTIALS to the service-account JSON path"
            )
        prospects = load_gsheet(sheet_id, args.sheet_range, creds_path)
    else:
        api_key = os.environ.get("CLAY_API_KEY")
        table_id = os.environ.get("CLAY_TABLE_ID")
        if not (api_key and table_id):
            sys.exit(
                "no source configured — pass --csv, --sheet, or set "
                "CLAY_SHEET_ID, or CLAY_API_KEY + CLAY_TABLE_ID"
            )
        prospects = load_clay(api_key, table_id)

    signaled = [p for p in prospects if p.priority is not None]
    state = load_state(STATE_FILE)
    to_alert = [p for p in signaled if should_alert(p, state)]
    to_alert.sort(key=lambda p: (p.priority, p.full_name))

    print(
        f"{len(prospects)} prospects loaded, {len(signaled)} with signals, "
        f"{len(to_alert)} new or changed since last run"
    )

    payload = build_alerts_payload(to_alert)

    if args.dry_run:
        for alert in payload["alerts"]:
            print(f"\n--- alert (owner={alert['owner_id'] or 'unowned'}) ---")
            print(alert["text"])
        if "digest" in payload:
            print("\n--- DIGEST ---")
            print(payload["digest"])
        return

    with ALERTS_OUT_FILE.open("w") as f:
        json.dump(payload, f, indent=2)
    n_alerts = len(payload["alerts"])
    suffix = " + digest" if "digest" in payload else ""
    print(f"wrote {n_alerts} alert(s){suffix} to {ALERTS_OUT_FILE}")

    for p in to_alert:
        record_alert(p, state)
    save_state(STATE_FILE, state)
    print(f"saved state to {STATE_FILE}")


if __name__ == "__main__":
    main()
