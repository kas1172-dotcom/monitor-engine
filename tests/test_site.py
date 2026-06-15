"""Tests for the static site builder."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from monitor_engine.models import (
    AnalyzedItem,
    Branding,
    Cadence,
    ClientConfig,
    CostCaps,
    Edition,
    EditionAnalysis,
    EditorialSynthesis,
    EscalatedItem,
    KeywordPrefilter,
    RunMeta,
    RunOutput,
    RssSource,
    ScoringRubric,
    SiteConfig,
    TierThresholds,
    WhatsDiff,
)
from monitor_engine.site.builder import build_site

# ─── Fixtures ─────────────────────────────────────────────────────────────

_NOW = datetime(2026, 6, 12, 9, 0, tzinfo=timezone.utc)


@pytest.fixture()
def config() -> ClientConfig:
    return ClientConfig(
        branding=Branding(name="Acme Monitor", accent_color="#E63946"),
        editions=[
            Edition(
                id="exec",
                label="Executive",
                audience_description="Executives",
                analysis_instructions="Strategic focus",
                categories=["Finance", "Regulation"],
            ),
            Edition(
                id="ops",
                label="Operations",
                audience_description="Ops teams",
                analysis_instructions="Operational focus",
                categories=["Safety", "Logistics"],
            ),
        ],
        scoring_rubric=ScoringRubric(
            thresholds=TierThresholds(),
            never_discard=[],
        ),
        sources=[RssSource(type="rss", id="s1", name="Feed", url="https://example.com/feed")],
        keyword_prefilter=KeywordPrefilter(include=["test"]),
        cadence=Cadence(cron="0 6 * * 1"),
        cost_caps=CostCaps(),
    )


def _item(item_id: str, tier: int = 2) -> AnalyzedItem:
    return AnalyzedItem(
        item_id=item_id,
        title=f"Title {item_id}",
        url=f"https://example.com/{item_id}",
        source_id="Feed",
        published_at=_NOW,
        collected_at=_NOW,
        tier=tier,
        per_edition={
            "exec": EditionAnalysis(
                relevance_score=75,
                so_what="Matters strategically.",
                now_what="Review and respond.",
                categories=["Finance"],
            )
        },
    )


@pytest.fixture()
def run_output() -> RunOutput:
    return RunOutput(
        meta=RunMeta(
            run_id="20260612T090000-aaaa",
            run_at=_NOW,
            items_collected=5,
            items_after_prefilter=4,
            items_analyzed=3,
            estimated_cost_usd=0.0042,
            engine_version="0.1.0",
        ),
        items=[_item("a", tier=1), _item("b", tier=2), _item("c", tier=3)],
        whats_new=WhatsDiff(
            new_tier_1=["a"],
            new_tier_2=["b"],
            escalated=[EscalatedItem(item_id="b", previous_tier=3, current_tier=2)],
            dropped=[],
            deadline_imminent=[],
        ),
        editorial=EditorialSynthesis(
            theme_of_week="Budget constraints dominate.",
            editors_note="Three programs saw funding shifts.",
            whats_new_digest="New allocations signal modernization.",
        ),
    )


# ─── Output files ─────────────────────────────────────────────────────────

class TestBuildSiteOutputFiles:
    def test_creates_index_html(self, config, run_output, tmp_path):
        build_site(run_output, config, tmp_path)
        assert (tmp_path / "index.html").exists()

    def test_creates_data_json(self, config, run_output, tmp_path):
        build_site(run_output, config, tmp_path)
        assert (tmp_path / "run_output.json").exists()

    def test_custom_data_filename(self, config, run_output, tmp_path):
        build_site(run_output, config, tmp_path, data_filename="brief.json")
        assert (tmp_path / "brief.json").exists()
        assert not (tmp_path / "run_output.json").exists()

    def test_creates_output_dir_if_missing(self, config, run_output, tmp_path):
        target = tmp_path / "deep" / "nested" / "output"
        build_site(run_output, config, target)
        assert (target / "index.html").exists()


# ─── JSON artifact content ────────────────────────────────────────────────

class TestDataJson:
    def test_json_is_parseable(self, config, run_output, tmp_path):
        build_site(run_output, config, tmp_path)
        data = json.loads((tmp_path / "run_output.json").read_text())
        assert isinstance(data, dict)

    def test_site_config_embedded(self, config, run_output, tmp_path):
        build_site(run_output, config, tmp_path)
        data = json.loads((tmp_path / "run_output.json").read_text())
        assert "site_config" in data
        assert data["site_config"] is not None

    def test_site_config_name_matches_branding(self, config, run_output, tmp_path):
        build_site(run_output, config, tmp_path)
        data = json.loads((tmp_path / "run_output.json").read_text())
        assert data["site_config"]["name"] == "Acme Monitor"

    def test_site_config_accent_color(self, config, run_output, tmp_path):
        build_site(run_output, config, tmp_path)
        data = json.loads((tmp_path / "run_output.json").read_text())
        assert data["site_config"]["accent_color"] == "#E63946"

    def test_site_config_editions_present(self, config, run_output, tmp_path):
        build_site(run_output, config, tmp_path)
        data = json.loads((tmp_path / "run_output.json").read_text())
        editions = data["site_config"]["editions"]
        assert len(editions) == 2
        assert editions[0]["id"] == "exec"
        assert editions[0]["label"] == "Executive"
        assert "Finance" in editions[0]["categories"]

    def test_site_config_edition_categories(self, config, run_output, tmp_path):
        build_site(run_output, config, tmp_path)
        data = json.loads((tmp_path / "run_output.json").read_text())
        ops = next(e for e in data["site_config"]["editions"] if e["id"] == "ops")
        assert "Safety" in ops["categories"]
        assert "Logistics" in ops["categories"]

    def test_items_preserved_in_json(self, config, run_output, tmp_path):
        build_site(run_output, config, tmp_path)
        data = json.loads((tmp_path / "run_output.json").read_text())
        assert len(data["items"]) == 3

    def test_whats_new_preserved(self, config, run_output, tmp_path):
        build_site(run_output, config, tmp_path)
        data = json.loads((tmp_path / "run_output.json").read_text())
        assert "a" in data["whats_new"]["new_tier_1"]
        assert "b" in data["whats_new"]["new_tier_2"]

    def test_editorial_preserved(self, config, run_output, tmp_path):
        build_site(run_output, config, tmp_path)
        data = json.loads((tmp_path / "run_output.json").read_text())
        assert data["editorial"]["theme_of_week"] == "Budget constraints dominate."

    def test_importance_score_in_items(self, config, run_output, tmp_path):
        build_site(run_output, config, tmp_path)
        data = json.loads((tmp_path / "run_output.json").read_text())
        # importance_score is a computed_field — must be serialised
        assert "importance_score" in data["items"][0]
        assert data["items"][0]["importance_score"] == 75

    def test_data_url_placeholder_replaced_in_html(self, config, run_output, tmp_path):
        build_site(run_output, config, tmp_path, data_filename="brief.json")
        html = (tmp_path / "index.html").read_text()
        assert "DATA_FILENAME_PLACEHOLDER" not in html
        assert "brief.json" in html

    def test_original_run_output_not_mutated(self, config, run_output, tmp_path):
        original_site_config = run_output.site_config
        build_site(run_output, config, tmp_path)
        assert run_output.site_config is original_site_config  # unchanged


# ─── HTML structure ───────────────────────────────────────────────────────

class TestHtmlStructure:
    def _html(self, config, run_output, tmp_path, **kw):
        build_site(run_output, config, tmp_path, **kw)
        return (tmp_path / "index.html").read_text()

    def test_html_doctype(self, config, run_output, tmp_path):
        html = self._html(config, run_output, tmp_path)
        assert html.startswith("<!doctype html>")

    def test_viewport_meta_present(self, config, run_output, tmp_path):
        html = self._html(config, run_output, tmp_path)
        assert 'name="viewport"' in html
        assert "width=device-width" in html

    def test_css_inlined_not_linked(self, config, run_output, tmp_path):
        html = self._html(config, run_output, tmp_path)
        # No external stylesheet link
        assert '<link rel="stylesheet"' not in html
        assert '<link rel="stylesheet' not in html
        # Has an inline style block
        assert "<style>" in html

    def test_js_inlined_not_external(self, config, run_output, tmp_path):
        html = self._html(config, run_output, tmp_path)
        # No script src attribute pointing to a .js file
        assert 'script src=' not in html.lower().replace('"', '').replace("'", '')
        # Has an inline script block with our code
        assert "<script>" in html

    def test_css_placeholder_replaced(self, config, run_output, tmp_path):
        html = self._html(config, run_output, tmp_path)
        assert "/* STYLE_PLACEHOLDER */" not in html

    def test_js_placeholder_replaced(self, config, run_output, tmp_path):
        html = self._html(config, run_output, tmp_path)
        assert "/* SCRIPT_PLACEHOLDER */" not in html

    def test_known_css_rules_present(self, config, run_output, tmp_path):
        html = self._html(config, run_output, tmp_path)
        assert ".site-header" in html
        assert ".full-card" in html
        assert ".compact-row" in html
        assert ".tier3-drawer" in html

    def test_known_js_functions_present(self, config, run_output, tmp_path):
        html = self._html(config, run_output, tmp_path)
        assert "function render()" in html
        assert "function applyBranding" in html
        assert "function filteredItems" in html

    def test_html_has_loading_screen(self, config, run_output, tmp_path):
        html = self._html(config, run_output, tmp_path)
        assert 'id="loading-screen"' in html

    def test_html_has_tier_sections(self, config, run_output, tmp_path):
        html = self._html(config, run_output, tmp_path)
        assert 'id="tier1-cards"' in html
        assert 'id="tier2-cards"' in html
        assert 'id="tier3-list"' in html

    def test_html_has_edition_nav(self, config, run_output, tmp_path):
        html = self._html(config, run_output, tmp_path)
        assert 'id="edition-nav"' in html

    def test_html_has_whats_new_section(self, config, run_output, tmp_path):
        html = self._html(config, run_output, tmp_path)
        assert 'id="whats-new"' in html

    def test_no_hardcoded_brand_name_in_template(self, config, run_output, tmp_path):
        # The template itself must not bake in the client name — it comes from data JS
        html = self._html(config, run_output, tmp_path)
        # site-title element should be empty in the static HTML (JS fills it)
        assert 'id="site-title"></h1>' in html or 'id="site-title"></h1>' in html.replace('\n', '')

    def test_data_url_in_script_tag(self, config, run_output, tmp_path):
        html = self._html(config, run_output, tmp_path, data_filename="weekly.json")
        assert "window.__DATA_URL = 'weekly.json'" in html


# ─── SiteConfig model ─────────────────────────────────────────────────────

class TestSiteConfigModel:
    def test_site_config_round_trips_json(self):
        cfg = SiteConfig(
            name="Test",
            accent_color="#123456",
            editions=[],
        )
        serialised = cfg.model_dump_json()
        recovered = SiteConfig.model_validate_json(serialised)
        assert recovered.name == "Test"
        assert recovered.accent_color == "#123456"

    def test_build_site_does_not_require_run_output_site_config_pre_set(self, config, tmp_path):
        # RunOutput has site_config=None by default; build_site must set it
        ro = RunOutput(
            meta=RunMeta(
                run_id="x", run_at=_NOW,
                items_collected=0, items_after_prefilter=0, items_analyzed=0,
                estimated_cost_usd=None, engine_version="0.1.0",
            ),
            items=[],
            whats_new=WhatsDiff(new_tier_1=[], new_tier_2=[], escalated=[], dropped=[]),
        )
        assert ro.site_config is None
        build_site(ro, config, tmp_path)
        data = json.loads((tmp_path / "run_output.json").read_text())
        assert data["site_config"]["name"] == "Acme Monitor"
