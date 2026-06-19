"""
Survey → intake → config round-trip.

Drives the survey's pure core (docs/intake/survey.js) via Node with BTX
defense-manufacturer answers, then feeds the generated intake through the real
tooling/scaffold.py and asserts a valid ClientConfig with the profile intact —
i.e. the survey actually generates a usable config, not just JSON.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from monitor_engine.models import ClientConfig
from tooling.scaffold import scaffold

_HERE = Path(__file__).parent
_HARNESS = _HERE / "frontend" / "intake_harness.mjs"
_NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(
    _NODE is None and not os.environ.get("CI_REQUIRE_FRONTEND"),
    reason="node not installed (set CI_REQUIRE_FRONTEND=1 to enforce)",
)

# BTX: a build-to-print precision-machining defense manufacturer.
_BTX_ANSWERS = {
    "monitor_name": "BTX Aerospace & Defense Monitor",
    "industry": "defense manufacturing",
    "accent_color": "#1E3A8A",
    "editions": [
        {"label": "Business Development", "role": "BD team",
         "matters": "new programs and funding BTX could bid on", "topics": "Contracts\nFunding"},
        {"label": "Executive Brief", "role": "executives",
         "matters": "strategic and supply-chain risk", "topics": "Policy\nSupply chain"},
    ],
    "sources": ["Government contract notices", "Federal Register", "Company press releases"],
    "source_other": "Defense trade press",
    "profile": {
        "capabilities": "precision machining\nbuild-to-print manufacturing\n5-axis CNC machining",
        "certifications": "AS9100\nITAR",
        "customer_types": "defense primes\nTier 1 suppliers",
        "geographic_focus": "United States",
        "strategic_goals": "grow defense revenue",
        "risks": "supply-chain disruption\nITAR compliance",
        "named_entities": {
            "customers": "Lockheed Martin\nRTX\nNorthrop Grumman",
            "competitors": "",
            "agencies": "Department of Defense\nDCMA",
            "programs": "F-35\nB-21",
        },
    },
    "must_not_miss": "recall\naward",
    "noise_to_exclude": "obituary",
    "cadence": {"frequency": "weekly", "day": "monday", "hour_local": "7", "timezone": "America/New_York"},
}


def _run_survey(answers: dict) -> dict:
    if _NODE is None:
        pytest.fail("node is required to run the survey harness (CI_REQUIRE_FRONTEND=1)")
    answers_path = _HERE / "_tmp_answers.json"
    answers_path.write_text(json.dumps(answers), encoding="utf-8")
    try:
        proc = subprocess.run(
            [_NODE, str(_HARNESS), str(answers_path)],
            capture_output=True, text=True, timeout=60,
        )
        assert proc.returncode == 0, f"harness failed:\n{proc.stderr}"
        return json.loads(proc.stdout)
    finally:
        answers_path.unlink(missing_ok=True)


def test_btx_answers_generate_valid_config():
    result = _run_survey(_BTX_ANSWERS)
    assert result["errors"] == []

    intake = result["intake"]
    # The survey emits intake.json; scaffold.py (the single mapper) makes the config.
    scaffolded = scaffold(intake)
    config = ClientConfig.model_validate(scaffolded["config"])

    assert config.branding.name == "BTX Aerospace & Defense Monitor"
    assert [e.label for e in config.editions] == ["Business Development", "Executive Brief"]

    # Profile survived the round-trip and is structured.
    assert config.profile is not None
    assert "precision machining" in config.profile.capabilities
    assert "AS9100" in config.profile.certifications
    assert "Lockheed Martin" in config.profile.named_entities.customers
    assert "F-35" in config.profile.named_entities.programs
    # Industry answer folded into the profile.
    assert "defense manufacturing" in config.profile.industries_served

    # Named entities widened the prefilter; the requested-other source became coverage.
    assert "Lockheed Martin" in config.keyword_prefilter.include
    assert any(b["name"] == "Defense trade press" for b in scaffolded["source_briefs"])


def test_missing_required_fields_reported():
    bad = {**_BTX_ANSWERS, "monitor_name": "", "editions": [], "sources": [], "source_other": ""}
    result = _run_survey(bad)
    errors = " ".join(result["errors"]).lower()
    assert "monitor name" in errors
    assert "edition" in errors
    assert "source" in errors
