"""Tests for deterministic client-feedback ingestion."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from monitor_engine.feedback import (
    apply_to_analyzed,
    apply_to_config,
    filter_muted_sources,
    load_feedback,
)
from monitor_engine.models import (
    AnalyzedItem,
    Branding,
    Cadence,
    ClientConfig,
    CostCaps,
    Edition,
    EditionAnalysis,
    Feedback,
    KeywordPrefilter,
    RawItem,
    ScoringRubric,
    TierThresholds,
)


def _config(**over) -> ClientConfig:
    base = dict(
        branding=Branding(name="T", accent_color="#123456"),
        editions=[Edition(id="e", label="E", audience_description="a", analysis_instructions="i", categories=[])],
        scoring_rubric=ScoringRubric(thresholds=TierThresholds(), never_discard=["recall"]),
        sources=[],
        keyword_prefilter=KeywordPrefilter(include=["fda"], exclude=["obituary"]),
        cadence=Cadence(cron="0 6 * * 1"),
        cost_caps=CostCaps(),
    )
    base.update(over)
    return ClientConfig(**base)


def _raw(rid: str, source: str) -> RawItem:
    return RawItem(
        id=rid, title="t", summary="s", url=f"https://e.com/{rid}",
        published_date=datetime(2026, 6, 10, tzinfo=timezone.utc), discovery_date=datetime.now(timezone.utc),
        source_name=source, source_type="rss",
    )


def _analyzed(item_id: str, url: str, tier: int = 2) -> AnalyzedItem:
    return AnalyzedItem(
        item_id=item_id, title="t", url=url, source_id="src",
        published_at=datetime(2026, 6, 10, tzinfo=timezone.utc), collected_at=datetime.now(timezone.utc),
        tier=tier, per_edition={"e": EditionAnalysis(relevance_score=60, so_what="x", now_what="y", categories=[])},
    )


class TestLoad:
    def test_missing_file_is_empty(self, tmp_path):
        fb = load_feedback(tmp_path / "nope.json")
        assert fb == Feedback()

    def test_valid_file(self, tmp_path):
        p = tmp_path / "feedback.json"
        p.write_text('{"mute_terms": ["x"], "pin_urls": ["https://e.com/1"]}')
        fb = load_feedback(p)
        assert fb.mute_terms == ["x"] and fb.pin_urls == ["https://e.com/1"]

    def test_malformed_file_is_empty_not_raise(self, tmp_path):
        p = tmp_path / "feedback.json"
        p.write_text("{not json")
        assert load_feedback(p) == Feedback()


class TestApplyToConfig:
    def test_boost_terms_added_to_include_and_never_discard(self):
        cfg = apply_to_config(_config(), Feedback(boost_terms=["merger"]))
        assert "merger" in cfg.keyword_prefilter.include
        assert "merger" in cfg.scoring_rubric.never_discard
        assert "fda" in cfg.keyword_prefilter.include  # original preserved

    def test_mute_terms_added_to_exclude(self):
        cfg = apply_to_config(_config(), Feedback(mute_terms=["sports"]))
        assert "sports" in cfg.keyword_prefilter.exclude
        assert "obituary" in cfg.keyword_prefilter.exclude

    def test_dedup(self):
        cfg = apply_to_config(_config(), Feedback(boost_terms=["fda"]))  # already in include
        assert cfg.keyword_prefilter.include.count("fda") == 1

    def test_empty_is_noop_same_object(self):
        cfg = _config()
        assert apply_to_config(cfg, Feedback()) is cfg


class TestMuteSources:
    def test_drops_muted_source(self):
        items = [_raw("1", "MedCity"), _raw("2", "FDA")]
        out = filter_muted_sources(items, Feedback(mute_sources=["MedCity"]))
        assert [i.id for i in out] == ["2"]

    def test_empty_noop(self):
        items = [_raw("1", "FDA")]
        assert filter_muted_sources(items, Feedback()) == items


class TestApplyToAnalyzed:
    def test_suppress_drops_by_normalized_url(self):
        items = [_analyzed("a", "https://e.com/1"), _analyzed("b", "https://e.com/2")]
        out = apply_to_analyzed(items, Feedback(suppress_urls=["https://e.com/1/"]))  # trailing slash
        assert [i.item_id for i in out] == ["b"]

    def test_pin_forces_tier_1(self):
        items = [_analyzed("a", "https://e.com/1", tier=3)]
        out = apply_to_analyzed(items, Feedback(pin_urls=["https://e.com/1"]))
        assert out[0].tier == 1

    def test_pin_already_tier1_unchanged(self):
        items = [_analyzed("a", "https://e.com/1", tier=1)]
        out = apply_to_analyzed(items, Feedback(pin_urls=["https://e.com/1"]))
        assert out[0].tier == 1

    def test_empty_noop(self):
        items = [_analyzed("a", "https://e.com/1")]
        assert apply_to_analyzed(items, Feedback()) == items
