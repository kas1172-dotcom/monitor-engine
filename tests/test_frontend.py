"""
Frontend smoke test: execute the real site/_assets/app.js against a sample
run_output.json in a headless Node DOM shim and assert items land in the
correct tier buckets.

This guards the data↔frontend contract: tier is the integer 1/2/3 (defined in
models.py as ``tier: Literal[1, 2, 3]``); the frontend groups on those integers
and maps them to display labels only at the presentation layer. A regression
that breaks grouping (wrong key, string comparison, a crash in render) makes
items silently vanish in the browser — exactly what this test catches in CI.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

_HERE = Path(__file__).parent
_RENDER_MJS = _HERE / "frontend" / "render.mjs"
_FIXTURE = _HERE / "frontend" / "fixtures" / "sample_run_output.json"

_NODE = shutil.which("node")
# CI sets CI_REQUIRE_FRONTEND=1: a missing node must then FAIL, not skip.
# A skipped test is a green build with no protection — the exact failure mode
# (broken dashboard, passing pipeline) this suite exists to catch.
_REQUIRE_FRONTEND = os.environ.get("CI_REQUIRE_FRONTEND") == "1"
pytestmark = pytest.mark.skipif(
    _NODE is None and not _REQUIRE_FRONTEND,
    reason="node not installed (set CI_REQUIRE_FRONTEND=1 to enforce)",
)


def _render(json_path: Path, search_query: str | None = None) -> dict:
    """Run app.js against json_path via the Node harness; return bucket counts.
    If search_query is given, the harness also types it into the search box and
    reports the post-filter item count as 'searchAfter'."""
    assert _NODE is not None, (
        "node is required to run the frontend smoke tests "
        "(CI_REQUIRE_FRONTEND=1 is set) but was not found on PATH"
    )
    cmd = [_NODE, str(_RENDER_MJS), str(json_path)]
    if search_query is not None:
        cmd.append(search_query)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f"node harness failed:\n{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.fixture(scope="module")
def fixture_data() -> dict:
    return json.loads(_FIXTURE.read_text())


@pytest.fixture(scope="module")
def rendered() -> dict:
    return _render(_FIXTURE)


def test_fixture_tiers_are_integers(fixture_data):
    # The contract: tier is an int, never a string label.
    for item in fixture_data["items"]:
        assert isinstance(item["tier"], int)
        assert item["tier"] in (1, 2, 3)


def test_every_item_renders_in_some_bucket(rendered, fixture_data):
    assert rendered["totalRendered"] == len(fixture_data["items"])
    assert rendered["totalRendered"] == 12


def test_items_land_in_correct_tier_buckets(rendered, fixture_data):
    expected = {1: 0, 2: 0, 3: 0}
    for item in fixture_data["items"]:
        expected[item["tier"]] += 1
    assert rendered["tier1"] == expected[1]
    assert rendered["tier2"] == expected[2]
    assert rendered["tier3"] == expected[3]


def test_tier3_drawer_count_matches(rendered, fixture_data):
    t3 = sum(1 for i in fixture_data["items"] if i["tier"] == 3)
    assert int(rendered["tier3Count"]) == t3


def test_boot_completed_without_crashing(rendered):
    # loading screen only hides at the end of boot(); if render() threw
    # (e.g. the esc() shadowing bug), this stays False.
    assert rendered["loadingHidden"] is True


def test_branding_applied_from_data(rendered, fixture_data):
    assert rendered["title"] == fixture_data["site_config"]["name"]


# ─── In-depth analysis ─────────────────────────────────────────────────────

@pytest.mark.parametrize("tier", ["tier1", "tier2", "tier3"])
def test_indepth_button_renders_on_every_tier(rendered, tier):
    assert rendered["deep"][tier]["hasButton"] is True


@pytest.mark.parametrize("tier", ["tier1", "tier2", "tier3"])
def test_indepth_panel_populates_from_precomputed_data(rendered, fixture_data, tier):
    # Clicking the button must reveal the deep_analysis sections precomputed
    # at pipeline time and embedded in the artifact.
    expected_sections = len(fixture_data["site_config"]["deep_analysis_sections"])
    assert rendered["deep"][tier]["sectionCount"] == expected_sections
    assert expected_sections == 4


def test_fixture_items_carry_deep_analysis(fixture_data):
    for item in fixture_data["items"]:
        assert item["deep_analysis"] is not None
        assert "sections" in item["deep_analysis"]


# ─── Keyword search ────────────────────────────────────────────────────────

def test_search_narrows_to_matching_items():
    # Fixture titles are "Sample item N (tier T)"; "(tier 1)" matches only the
    # three tier-1 items. Confirms the search input is wired into filteredItems.
    rendered = _render(_FIXTURE, search_query="(tier 1)")
    assert rendered["searchAfter"] == 3

def test_search_no_match_hides_all():
    rendered = _render(_FIXTURE, search_query="zzzznomatch")
    assert rendered["searchAfter"] == 0

def test_no_search_shows_all(rendered, fixture_data):
    # Baseline (no query): every item renders, proving search defaults to inert.
    assert rendered["tier1"] + rendered["tier2"] + rendered["tier3"] == len(fixture_data["items"])


# ─── Broken-link handling ──────────────────────────────────────────────────

def test_http_url_renders_as_anchor(rendered):
    # Fixture items have https URLs → title is a real <a>.
    assert rendered["tier1TitleTag"] == "A"

def test_bare_reference_renders_as_span_not_dead_link(fixture_data, tmp_path):
    # An item whose URL is a bare id (e.g. an openFDA recall number) must render
    # as a non-clickable <span>, never an <a> with a dead href.
    data = json.loads(json.dumps(fixture_data))  # deep copy
    data["items"][0]["url"] = "D-0558-2026"       # bare recall id, not a URL
    p = tmp_path / "broken.json"
    p.write_text(json.dumps(data))
    rendered = _render(p)
    assert rendered["tier1TitleTag"] == "SPAN"
