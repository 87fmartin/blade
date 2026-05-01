"""Tests for blade.py — covers signal logic, parsing, dedup, and message shape."""

from __future__ import annotations

import json

import pytest

import blade
from blade import (
    Prospect,
    _normalize_linkedin,
    _parse_rows,
    build_alert_text,
    build_alerts_payload,
    build_digest,
    format_signal_lines,
    record_alert,
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
    email="",
):
    return Prospect(name, title, org, linkedin, change, openings, owner, email)


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
                    "Work Email": "alice@acme.com",
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
        assert p.email == "alice@acme.com"
        assert p.job_change_raw == "Promotion"
        assert p.job_openings == 12
        assert p.owner_id == "111"

    def test_missing_email_becomes_empty(self):
        [p] = _parse_rows([{}])
        assert p.email == ""

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


# ---------- message + payload shape ----------


class TestBuildAlertText:
    def test_includes_all_fields(self):
        p = make(
            name="Alice",
            title="VP Eng",
            org="Acme",
            linkedin="https://l/alice",
            email="alice@acme.com",
            change="Promotion",
            openings=10,
        )
        msg = build_alert_text(p)
        assert "Priority 1" in msg
        assert "Job Change/Promotion + Hiring Spree" in msg
        assert "<https://l/alice|Alice>" in msg
        assert "VP Eng" in msg
        assert "Acme" in msg
        assert "alice@acme.com" in msg
        assert "Promotion" in msg
        assert "Hiring spree" in msg
        assert "10 open" in msg
        assert "less than 90 days ago" in msg

    def test_headline_per_priority(self):
        # P1: both signals
        p1 = build_alert_text(make(change="Promotion", openings=10))
        assert "Job Change/Promotion + Hiring Spree (Priority 1)" in p1
        # P2: change only
        p2 = build_alert_text(make(change="Job Change"))
        assert "Job Change/Promotion Only (Priority 2)" in p2
        # P3: hiring only
        p3 = build_alert_text(make(openings=8))
        assert "Hiring Spree Only (Priority 3)" in p3

    def test_distinguishes_promotion_vs_job_change_in_body(self):
        # Headline contains both terms now; check the body lines directly.
        promo_lines = format_signal_lines(make(change="Promotion"))
        change_lines = format_signal_lines(make(change="Job Change"))
        assert any(line.startswith("Promotion —") for line in promo_lines)
        assert not any(line.startswith("Job Change —") for line in promo_lines)
        assert any(line.startswith("Job Change —") for line in change_lines)
        assert not any(line.startswith("Promotion —") for line in change_lines)

    def test_links_name_to_linkedin_when_present(self):
        msg = build_alert_text(make(linkedin="https://l/alice", change="Promotion"))
        assert "<https://l/alice|Alice>" in msg

    def test_no_link_when_linkedin_missing(self):
        msg = build_alert_text(make(linkedin="", change="Promotion"))
        # Plain name, no Slack link wrapper
        assert "<http" not in msg
        assert "Alice" in msg

    def test_omits_email_line_when_missing(self):
        without = build_alert_text(make(email="", change="Promotion"))
        with_email = build_alert_text(make(email="alice@acme.com", change="Promotion"))
        # Email line is one extra newline-separated line
        assert with_email.count("\n") == without.count("\n") + 1
        assert "alice@acme.com" not in without


class TestBuildDigest:
    def test_groups_by_priority(self):
        prospects = [
            make(name="Alpha", linkedin="https://l/a", change="Promotion", openings=10),
            make(name="Bravo", linkedin="https://l/b", openings=6),
            make(name="Charlie", linkedin="https://l/c", change="Job Change"),
        ]
        d = build_digest(prospects)
        # Priority headlines match the per-alert format for consistency.
        assert "Job Change/Promotion + Hiring Spree (Priority 1)" in d
        assert "Job Change/Promotion Only (Priority 2)" in d
        assert "Hiring Spree Only (Priority 3)" in d
        assert "3 signals fired" in d
        for name in ("Alpha", "Bravo", "Charlie"):
            assert d.count(name) == 1
        # Names link to LinkedIn in the digest too
        assert "<https://l/a|Alpha>" in d

    def test_omits_empty_tiers(self):
        prospects = [make(change="Promotion")]  # only P2
        d = build_digest(prospects)
        assert "Priority 2" in d
        assert "Priority 1" not in d
        assert "Priority 3" not in d


class TestBuildAlertsPayload:
    def test_full_payload_shape(self):
        p = make(change="Promotion", openings=10, owner="111")
        payload = build_alerts_payload([p])

        assert "run_at" in payload
        assert "1 signal fired" in payload["digest"]

        [alert] = payload["alerts"]
        assert "to" not in alert
        assert alert["owner_id"] == "111"
        assert alert["prospect"] == "Alice"
        assert alert["priority"] == 1
        assert "Alice" in alert["text"]

    def test_empty_input_omits_digest(self):
        payload = build_alerts_payload([])
        assert payload["alerts"] == []
        assert "digest" not in payload

    def test_unowned_prospect_owner_id_is_none(self):
        p = make(change="Promotion", owner="")
        payload = build_alerts_payload([p])
        assert payload["alerts"][0]["owner_id"] is None

    def test_serializes_to_json(self):
        # Sanity check: payload must be JSON-serializable (no datetimes, etc).
        payload = build_alerts_payload([make(change="Promotion")])
        json.dumps(payload)


# ---------- end-to-end ----------


class TestCsvEndToEnd:
    def test_csv_to_payload(self, tmp_path):
        csv_path = tmp_path / "p.csv"
        csv_path.write_text(
            "Full Name,Title,Org,LinkedIn URL,Promotion or Job Change,Job Openings,HubSpot Owner ID\n"
            "Alice,VP,Acme,linkedin.com/in/alice,Promotion,10,111\n"
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
        prospects = [make(change="Promotion", openings=10, owner="111")]
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
