"""Tests for the pipeline orchestrator. Collection and LLM calls are mocked."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from monitor_engine.collectors.base import CollectionResult
from monitor_engine.models import RawItem, SourceHealth
from monitor_engine.pipeline import apply_prefilter, run_pipeline

_NOW = datetime(2026, 6, 12, 9, 0, tzinfo=timezone.utc)


def _config_dict() -> dict:
    return {
        "branding": {"name": "Test Monitor", "accent_color": "#0066CC"},
        "editions": [
            {
                "id": "exec",
                "label": "Executive",
                "audience_description": "Executives",
                "analysis_instructions": "Strategic focus",
                "categories": ["Finance"],
            }
        ],
        "scoring_rubric": {
            "thresholds": {"tier_1_min": 80, "tier_2_min": 50, "tier_3_min": 20},
            "never_discard": [],
        },
        "sources": [
            {"type": "rss", "id": "s1", "name": "Feed", "url": "https://example.com/feed"}
        ],
        "keyword_prefilter": {"include": ["budget"], "exclude": []},
        "cadence": {"cron": "0 6 * * 1"},
        "cost_caps": {"max_items_per_run": 50, "max_output_tokens_per_run": 8000},
    }


@pytest.fixture()
def config_path(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps(_config_dict()))
    return p


def _raw(item_id: str, title: str = "Budget news item") -> RawItem:
    return RawItem(
        id=item_id,
        title=title,
        summary="Some budget summary.",
        url=f"https://example.com/{item_id}",
        published_date=_NOW,
        date_unknown=False,
        discovery_date=_NOW,
        source_name="Feed",
        source_type="rss",
    )


def _collection(items: list[RawItem]) -> CollectionResult:
    return CollectionResult(
        items=items,
        health={
            "s1": SourceHealth(
                source_id="s1",
                items_collected=len(items),
                zero_results=len(items) == 0,
            )
        },
    )


# ─── Prefilter ─────────────────────────────────────────────────────────────

class TestApplyPrefilter:
    def test_include_match_passes(self):
        items = [_raw("a", title="Budget approved")]
        assert len(apply_prefilter(items, ["budget"], [])) == 1

    def test_no_match_drops(self):
        items = [_raw("a", title="Sports results")]
        items[0].summary = "nothing relevant"
        assert apply_prefilter(items, ["budget"], []) == []

    def test_exclude_overrides_include(self):
        items = [_raw("a", title="Budget for sports stadium")]
        assert apply_prefilter(items, ["budget"], ["sports"]) == []

    def test_empty_include_passes_everything(self):
        items = [_raw("a", title="Anything at all")]
        items[0].summary = None
        assert len(apply_prefilter(items, [], [])) == 1


# ─── Analysis-failure guard ────────────────────────────────────────────────

class TestAnalysisFailureGuard:
    def _seed_artifacts(self, output_dir) -> dict[str, str]:
        """Pre-write sentinel artifacts simulating a previous successful run."""
        output_dir.mkdir(parents=True, exist_ok=True)
        sentinels = {
            "index.html": "<!-- previous site -->",
            "run_output.json": '{"previous": true}',
            "archive.json": '{"runs": [], "pinned": []}',
        }
        for name, content in sentinels.items():
            (output_dir / name).write_text(content)
        return sentinels

    def test_guard_fires_on_total_analysis_failure(self, config_path, tmp_path, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        output_dir = tmp_path / "out"
        sentinels = self._seed_artifacts(output_dir)

        scorer = MagicMock()
        scorer.analyze.return_value = ([], None, 0.01)  # every batch quarantined

        with patch("monitor_engine.pipeline.collect_all", return_value=_collection([_raw("a")])), \
             patch("monitor_engine.analysis.scorer.Scorer", return_value=scorer):
            with pytest.raises(SystemExit) as excinfo:
                run_pipeline(config_path, output_dir)

        assert excinfo.value.code == 1
        # Previous artifacts must be byte-for-byte untouched
        for name, content in sentinels.items():
            assert (output_dir / name).read_text() == content, name

    def test_guard_does_not_fire_on_skip_analysis(self, config_path, tmp_path):
        output_dir = tmp_path / "out"

        with patch("monitor_engine.pipeline.collect_all", return_value=_collection([_raw("a")])):
            result = run_pipeline(config_path, output_dir, skip_analysis=True)

        assert result.meta.items_after_prefilter == 1
        assert result.meta.items_analyzed == 0
        assert (output_dir / "index.html").exists()
        assert (output_dir / "run_output.json").exists()
        assert (output_dir / "archive.json").exists()

    def test_guard_does_not_fire_on_quiet_news_week(self, config_path, tmp_path, monkeypatch):
        # Prefilter legitimately produces zero items; no analysis, no error.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        output_dir = tmp_path / "out"
        items = [_raw("a", title="Sports results")]
        items[0].summary = "nothing matching the prefilter"

        scorer_cls = MagicMock()
        with patch("monitor_engine.pipeline.collect_all", return_value=_collection(items)), \
             patch("monitor_engine.analysis.scorer.Scorer", scorer_cls):
            result = run_pipeline(config_path, output_dir)

        scorer_cls.assert_not_called()  # analysis never invoked on empty input
        assert result.meta.items_after_prefilter == 0
        assert result.meta.items_analyzed == 0
        assert (output_dir / "index.html").exists()
        assert (output_dir / "run_output.json").exists()

    def test_successful_analysis_still_publishes(self, config_path, tmp_path, monkeypatch):
        # Control case: guard must not block a normal run.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        from monitor_engine.models import AnalyzedItem, EditionAnalysis

        analyzed = AnalyzedItem(
            item_id="a",
            title="Budget news item",
            url="https://example.com/a",
            source_id="Feed",
            published_at=_NOW,
            collected_at=_NOW,
            tier=1,
            per_edition={
                "exec": EditionAnalysis(
                    relevance_score=90, so_what="x", now_what="y", categories=["Finance"]
                )
            },
        )
        output_dir = tmp_path / "out"
        scorer = MagicMock()
        scorer.analyze.return_value = ([analyzed], None, 0.01)

        with patch("monitor_engine.pipeline.collect_all", return_value=_collection([_raw("a")])), \
             patch("monitor_engine.analysis.scorer.Scorer", return_value=scorer):
            result = run_pipeline(config_path, output_dir)

        assert result.meta.items_analyzed == 1
        assert (output_dir / "index.html").exists()
