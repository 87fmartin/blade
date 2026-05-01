"""Tests for blade.py — covers signal logic, parsing, dedup, and message shape."""

from __future__ import annotations

import json

import pytest

import blade
from blade import (
    Prospect,
    _normalize_linkedin,
    _parse_rows,
    build_alerts_payload,
    build_digest,
    build_dm,
    record_alert,
    resolve_slack_handle,
    should_alert,
    state_key,
)


def make(
    name="Alice",
    title="VP",
    org="Acme",
    linkedin="https://l/alice",
    change="",
    openings=0,
    owner="",
):
    return Prospect(name, title, org, linkedin, change, openings, owner)


# ---------- signal detection + priority ----------


class TestSignals:
    def test_no_signal(self):
        p = make()
        assert p.signals == []
        assert p.priority is None

    def test_promotion(self):
        p = make(change="Promotion")
        assert p.signals == ["promotion"]
        assert p.priority == 2

    def test_job_change(self):
        p = make(change="Job Change")
        assert p.signals == ["job_change"]
        assert p.priority == 2

    def test_promotion_case_insensitive(self):
        assert make(change="promotion").signals == ["promotion"]
        assert make(change="PROMOTION").signals == ["promotion"]
        assert make(change="  Promotion  ").signals == ["promotion"]

    def test_non_promotion_non_empty_treated_as_job_change(self):
        # Per blade.py logic: any non-empty value without "promotion" → job_change.
        assert make(change="new role").signals == ["job_change"]

    def test_hiring_spree_threshold(self):
        assert make(openings=4).signals == []
        assert make(openings=5).signals == ["hiring_spree"]
        assert make(openings=100).signals == ["hiring_spree"]

    def test_priority_1_both(self):
        p = make(change="Promotion", openings=10)
        assert set(p.signals) == {"promotion", "hiring_spree"}
        assert p.priority == 1

    def test_priority_2_change_only(self):
        assert make(change="Job Change", openings=2).priority == 2

    def test_priority_3_hiring_only(self):
        assert make(openings=8).priority == 3


# ---------- LinkedIn URL normalization ----------


class TestNormalizeLinkedin:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("", ""),
            ("   ", ""),
            ("https://linkedin.com/in/foo", "https://linkedin.com/in/foo"),
            ("http://linkedin.com/in/foo", "http://linkedin.com/in/foo"),
            ("linkedin.com/in/foo", "https://linkedin.com/in/foo"),
            ("/linkedin.com/in/foo", "https://linkedin.com/in/foo"),
            ("  linkedin.com/in/foo  ", "https://linkedin.com/in/foo"),
            ("www.linkedin.com/in/foo", "https://www.linkedin.com/in/foo"),
        ],
    )
    def test_normalize(self, raw, expected):
        assert _normalize_linkedin(raw) == expected


# ---------- row parsing ----------


class TestParseRows:
    def test_basic_row(self):
        [p] = _parse_rows(
            [
                {
                    "Full Name": "Alice",
                    "Title": "VP Eng",
                    "Org": "Acme",
                    "LinkedIn URL": "linkedin.com/in/alice",
                    "Promotion or Job Change": "Promotion",
                    "Job Openings": "12",
                    "HubSpot Owner ID": "111",
                }
            ]
        )
        assert p.full_name == "Alice"
        assert p.title == "VP Eng"
        assert p.org == "Acme"
        assert p.linkedin == "https://linkedin.com/in/alice"
        assert p.job_change_raw == "Promotion"
        assert p.job_openings == 12
        assert p.owner_id == "111"

    def test_blank_openings_becomes_zero(self):
        [p] = _parse_rows([{"Job Openings": ""}])
        assert p.job_openings == 0

    def test_non_numeric_openings_becomes_zero(self):
        [p] = _parse_rows([{"Job Openings": "many"}])
        assert p.job_openings == 0

    def test_missing_openings_becomes_zero(self):
        [p] = _parse_rows([{}])
        assert p.job_openings == 0

    def test_strips_whitespace_on_text_fields(self):
        [p] = _parse_rows(
            [
                {
                    "Full Name": "  Alice  ",
                    "Title": "  VP  ",
                    "HubSpot Owner ID": "  111  ",
                }
            ]
        )
        assert p.full_name == "Alice"
        assert p.title == "VP"
        assert p.owner_id == "111"


# ---------- dedup ----------


class TestStateKey:
    def test_uses_linkedin_when_present(self):
        assert state_key(make(linkedin="https://l/x")) == "https://l/x"

    def test_falls_back_to_name_org_lowercased(self):
        p = make(name="Alice", org="Acme", linkedin="")
        assert state_key(p) == "alice|acme"


class TestShouldAlert:
    def test_alerts_when_state_empty(self):
        assert should_alert(make(change="Promotion"), {}) is True

    def test_skips_when_signals_unchanged(self):
        p = make(change="Promotion")
        state = {"https://l/alice": {"signals": ["promotion"]}}
        assert should_alert(p, state) is False

    def test_alerts_when_signal_added(self):
        p = make(change="Promotion", openings=10)  # promo + hiring
        state = {"https://l/alice": {"signals": ["promotion"]}}
        assert should_alert(p, state) is True

    def test_alerts_when_signal_removed(self):
        p = make(change="Promotion", openings=2)  # promo only
        state = {"https://l/alice": {"signals": ["hiring_spree", "promotion"]}}
        assert should_alert(p, state) is True

    def test_alerts_when_signal_type_changes(self):
        # promotion → job_change
        p = make(change="Job Change")
        state = {"https://l/alice": {"signals": ["promotion"]}}
        assert should_alert(p, state) is True

    def test_unaffected_by_openings_count_change(self):
        # 5 openings last week, 12 this week — same signal type, no re-alert.
        p = make(openings=12)
        state = {"https://l/alice": {"signals": ["hiring_spree"]}}
        assert should_alert(p, state) is False


class TestRecordAlert:
    def test_records_sorted_signals_priority_and_timestamp(self):
        p = make(change="Promotion", openings=10)
        state: dict = {}
        record_alert(p, state)
        entry = state["https://l/alice"]
        assert entry["signals"] == ["hiring_spree", "promotion"]  # sorted
        assert entry["priority"] == 1
        assert "alerted_at" in entry


# ---------- owner → Slack handle ----------


class TestResolveSlackHandle:
    def test_mapped_owner(self):
        # Pick any populated mapping entry from config.
        owner_id, handle = next(iter(blade.OWNER_TO_SLACK.items()))
        assert resolve_slack_handle(owner_id) == handle

    def test_unmapped_falls_back(self):
        assert resolve_slack_handle("000nonexistent") == blade.FALLBACK_SLACK_HANDLE

    def test_empty_falls_back(self):
        assert resolve_slack_handle("") == blade.FALLBACK_SLACK_HANDLE


# ---------- message + payload shape ----------


class TestBuildDm:
    def test_includes_all_fields(self):
        p = make(
            name="Alice",
            title="VP Eng",
            org="Acme",
            linkedin="https://l/alice",
            change="Promotion",
            openings=10,
        )
        msg = build_dm(p)
        assert "Priority 1" in msg
        assert "Alice" in msg
        assert "VP Eng" in msg
        assert "Acme" in msg
        assert "Promotion" in msg
        assert "Hiring spree" in msg
        assert "10 open" in msg
        assert "https://l/alice" in msg

    def test_distinguishes_promotion_vs_job_change_in_text(self):
        promo = build_dm(make(change="Promotion"))
        change = build_dm(make(change="Job Change"))
        assert "Promotion" in promo and "Job change" not in promo
        assert "Job change" in change and "Promotion" not in change

    def test_omits_linkedin_line_when_missing(self):
        msg = build_dm(make(linkedin="", change="Promotion"))
        assert "LinkedIn" not in msg


class TestBuildDigest:
    def test_groups_by_priority(self):
        prospects = [
            make(name="Alpha", linkedin="https://l/a", change="Promotion", openings=10),
            make(name="Bravo", linkedin="https://l/b", openings=6),
            make(name="Charlie", linkedin="https://l/c", change="Job Change"),
        ]
        d = build_digest(prospects)
        assert "Priority 1" in d
        assert "Priority 2" in d
        assert "Priority 3" in d
        assert "3 signals fired" in d
        for name in ("Alpha", "Bravo", "Charlie"):
            assert d.count(name) == 1

    def test_omits_empty_tiers(self):
        prospects = [make(change="Promotion")]  # only P2
        d = build_digest(prospects)
        assert "Priority 2" in d
        assert "Priority 1" not in d
        assert "Priority 3" not in d


class TestBuildAlertsPayload:
    def test_full_payload_shape(self):
        # Use a real owner_id from config so handle resolution exercises the dict.
        owner_id, handle = next(iter(blade.OWNER_TO_SLACK.items()))
        p = make(change="Promotion", openings=10, owner=owner_id)
        payload = build_alerts_payload([p])

        assert "run_at" in payload
        assert payload["digest"]["channel"] == "#sales-signals"
        assert "1 signal fired" in payload["digest"]["text"]

        [dm] = payload["dms"]
        assert dm["to"] == handle
        assert dm["owner_id"] == owner_id
        assert dm["prospect"] == "Alice"
        assert dm["priority"] == 1
        assert "Alice" in dm["text"]

    def test_empty_input_omits_digest(self):
        payload = build_alerts_payload([])
        assert payload["dms"] == []
        assert "digest" not in payload

    def test_unowned_prospect_routes_to_fallback(self):
        p = make(change="Promotion", owner="")
        payload = build_alerts_payload([p])
        assert payload["dms"][0]["to"] == blade.FALLBACK_SLACK_HANDLE
        assert payload["dms"][0]["owner_id"] is None

    def test_serializes_to_json(self):
        # Sanity check: payload must be JSON-serializable (no datetimes, etc).
        payload = build_alerts_payload([make(change="Promotion")])
        json.dumps(payload)


# ---------- end-to-end ----------


class TestCsvEndToEnd:
    def test_csv_to_payload(self, tmp_path):
        owner_id, handle = next(iter(blade.OWNER_TO_SLACK.items()))
        csv_path = tmp_path / "p.csv"
        csv_path.write_text(
            "Full Name,Title,Org,LinkedIn URL,Promotion or Job Change,Job Openings,HubSpot Owner ID\n"
            f"Alice,VP,Acme,linkedin.com/in/alice,Promotion,10,{owner_id}\n"
            "Bob,IC,Beta,linkedin.com/in/bob,,2,\n"
            "Carol,Eng,Gamma,linkedin.com/in/carol,,8,\n"
        )
        prospects = blade.load_csv(csv_path)
        assert len(prospects) == 3

        signaled = [p for p in prospects if p.priority is not None]
        # Alice P1, Bob no signal, Carol P3
        assert sorted(p.priority for p in signaled) == [1, 3]
        assert {p.full_name for p in signaled} == {"Alice", "Carol"}
        # LinkedIn URLs got https:// prefix
        assert all(p.linkedin.startswith("https://") for p in prospects)

    def test_dedup_across_two_runs(self):
        # Same data twice → second run produces no alerts.
        prospects = [make(change="Promotion", openings=10, owner="11721652")]
        state: dict = {}
        first = [p for p in prospects if should_alert(p, state)]
        assert len(first) == 1
        for p in first:
            record_alert(p, state)

        second = [p for p in prospects if should_alert(p, state)]
        assert second == []

    def test_signal_change_triggers_re_alert(self):
        state: dict = {}
        # Week 1: hiring spree only.
        p1 = make(linkedin="https://l/alice", openings=10)
        record_alert(p1, state)
        # Week 2: hiring spree + promotion.
        p2 = make(linkedin="https://l/alice", change="Promotion", openings=10)
        assert should_alert(p2, state) is True
