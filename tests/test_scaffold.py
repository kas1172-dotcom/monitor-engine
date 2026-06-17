"""Tests for the deterministic intake → draft-config scaffolder."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from monitor_engine.models import ClientConfig
from tooling.scaffold import cadence_to_cron, scaffold, slugify

_HEALTHCARE_INTAKE = (
    Path(__file__).parent.parent / "clients" / "healthcare-regulatory" / "intake.json"
)


@pytest.fixture(scope="module")
def intake() -> dict:
    return json.loads(_HEALTHCARE_INTAKE.read_text())


@pytest.fixture(scope="module")
def result(intake) -> dict:
    return scaffold(intake)


# ─── Helpers ────────────────────────────────────────────────────────────────

class TestHelpers:
    def test_slugify(self):
        assert slugify("Policy") == "policy"
        assert slugify("Coverage & Payment") == "coverage-payment"
        assert slugify("  Mixed CASE!! ") == "mixed-case"

    @pytest.mark.parametrize("cadence,expected", [
        ({"frequency": "weekly", "day": "monday", "hour_local": 7}, "0 7 * * 1"),
        ({"frequency": "weekly", "day": "friday", "hour_local": 6}, "0 6 * * 5"),
        ({"frequency": "weekdays", "hour_local": 8}, "0 8 * * 1-5"),
        ({"frequency": "daily", "hour_local": 9}, "0 9 * * *"),
    ])
    def test_cadence_to_cron(self, cadence, expected):
        assert cadence_to_cron(cadence) == expected


# ─── Draft config validity ────────────────────────────────────────────────

class TestScaffoldOutput:
    def test_config_validates_against_clientconfig(self, result):
        ClientConfig.model_validate(result["config"])

    def test_sources_left_empty_for_agent(self, result):
        assert result["config"]["sources"] == []

    def test_source_briefs_carry_coverage(self, result, intake):
        briefs = result["source_briefs"]
        # one per coverage entry, plus one per paid source
        assert len(briefs) == len(intake["coverage"]) + len(intake["paid_sources"])
        assert any(b.get("auth_env_var") == "CONGRESS_API_KEY" for b in briefs)


# ─── Deterministic round-trip vs. the hand-built healthcare config ──────────

class TestRoundTrip:
    def test_branding(self, result):
        b = result["config"]["branding"]
        assert b["name"] == "Healthcare Regulatory & Market Monitor"
        assert b["accent_color"] == "#0F766E"

    def test_editions_ids_and_categories(self, result):
        eds = result["config"]["editions"]
        assert [e["id"] for e in eds] == ["policy", "consulting"]
        policy = next(e for e in eds if e["id"] == "policy")
        assert "Rulemaking" in policy["categories"]
        assert policy["analysis_instructions"].startswith("Prioritise federal legislation")

    def test_never_discard(self, result):
        nd = result["config"]["scoring_rubric"]["never_discard"]
        assert "qui tam" in nd and "Class I recall" in nd

    def test_prefilter(self, result):
        pf = result["config"]["keyword_prefilter"]
        assert "obituary" in pf["exclude"]
        # include derived from topics + named entities
        assert "FDA" in pf["include"] and "Rulemaking" in pf["include"]

    def test_cadence(self, result):
        cad = result["config"]["cadence"]
        assert cad["cron"] == "0 7 * * 1"
        assert cad["timezone"] == "America/New_York"

    def test_deep_analysis_sections(self, result):
        da = result["config"]["deep_analysis"]
        assert [s["id"] for s in da["sections"]] == [
            "background", "key_stakeholders", "scenarios", "recommended_actions",
        ]
        assert da["deep_batch_size"] == 3

    def test_cost_caps_default(self, result):
        assert result["config"]["cost_caps"] == {
            "max_items_per_run": 120, "max_output_tokens_per_run": 90000,
        }


# ─── Minimal intake still produces a valid config ──────────────────────────

def test_minimal_intake_is_valid():
    minimal = {
        "monitor_name": "Tiny Monitor",
        "audiences": [{"label": "Exec", "role": "Execs", "matters": "Big picture", "topics": ["News"]}],
        "cadence": {"frequency": "weekly", "day": "monday", "hour_local": 7, "timezone": "UTC"},
    }
    result = scaffold(minimal)
    ClientConfig.model_validate(result["config"])
    assert result["config"]["branding"]["accent_color"] == "#0F766E"  # default applied
    assert result["config"]["sources"] == []
