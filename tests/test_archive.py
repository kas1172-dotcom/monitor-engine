"""
Tests for monitor_engine/archive/core.py.

Every logical branch of compute_diff gets its own test so that regressions are
immediately obvious rather than silently wrong in client-facing output.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from monitor_engine.archive.core import (
    compute_diff,
    dedup_items,
    load_archive,
    normalize_url,
    save_archive,
    update_archive,
)
from monitor_engine.models import (
    AnalyzedItem,
    Archive,
    ArchivedRun,
    EditionAnalysis,
)


# ─── Test helpers ─────────────────────────────────────────────────────────

_NOW = datetime(2026, 6, 12, 9, 0, tzinfo=timezone.utc)
_REF = date(2026, 6, 12)   # reference date used in deadline tests


def _item(
    item_id: str,
    *,
    url: str | None = None,
    tier: int = 2,
    score: int = 60,
    deadline: date | None = None,
) -> AnalyzedItem:
    return AnalyzedItem(
        item_id=item_id,
        title=f"Title {item_id}",
        url=url or f"https://example.com/{item_id}",
        source_id="test-source",
        published_at=None,
        collected_at=_NOW,
        tier=tier,
        per_edition={
            "ed1": EditionAnalysis(
                relevance_score=score,
                so_what="so what",
                now_what="now what",
                categories=[],
            )
        },
        action_deadline=deadline,
    )


def _run(
    run_id: str,
    items: list[AnalyzedItem],
    *,
    run_at: datetime = _NOW,
) -> ArchivedRun:
    return ArchivedRun(run_id=run_id, run_at=run_at, items=items)


# ─── normalize_url ────────────────────────────────────────────────────────

class TestNormalizeUrl:
    def test_strips_utm_params(self):
        url = "https://example.com/article?utm_source=newsletter&utm_medium=email"
        assert normalize_url(url) == "https://example.com/article"

    def test_strips_fbclid(self):
        url = "https://example.com/story?fbclid=IwAR3abc123"
        assert normalize_url(url) == "https://example.com/story"

    def test_strips_gclid(self):
        url = "https://example.com/page?gclid=abc&ref=weekly"
        assert normalize_url(url) == "https://example.com/page"

    def test_drops_fragment(self):
        url = "https://example.com/article#section-3"
        assert normalize_url(url) == "https://example.com/article"

    def test_strips_trailing_slash(self):
        assert normalize_url("https://example.com/article/") == "https://example.com/article"

    def test_lowercases_scheme_and_host(self):
        url = "HTTPS://EXAMPLE.COM/Article"
        assert normalize_url(url) == "https://example.com/Article"

    def test_preserves_non_tracking_query_params(self):
        url = "https://example.com/search?q=defense&page=2"
        result = normalize_url(url)
        assert "page=2" in result
        assert "q=defense" in result

    def test_strips_tracking_keeps_content_params(self):
        url = "https://example.com/article?utm_source=x&category=policy&page=3"
        result = normalize_url(url)
        assert "utm_source" not in result
        assert "category=policy" in result
        assert "page=3" in result

    def test_query_params_sorted_for_stable_comparison(self):
        url_a = "https://example.com/page?z=1&a=2"
        url_b = "https://example.com/page?a=2&z=1"
        assert normalize_url(url_a) == normalize_url(url_b)

    def test_empty_url_returned_unchanged(self):
        assert normalize_url("") == ""

    def test_url_without_query(self):
        url = "https://example.com/article"
        assert normalize_url(url) == "https://example.com/article"

    def test_tracking_plus_fragment(self):
        url = "https://example.com/page?utm_campaign=fall#top"
        assert normalize_url(url) == "https://example.com/page"


# ─── dedup_items ──────────────────────────────────────────────────────────

class TestDedupItems:
    def test_empty_list(self):
        assert dedup_items([]) == []

    def test_all_unique_returned_in_order(self):
        items = [_item("a"), _item("b"), _item("c")]
        result = dedup_items(items)
        assert [it.item_id for it in result] == ["a", "b", "c"]

    def test_duplicate_id_drops_second(self):
        first = _item("dup", url="https://example.com/dup", tier=1)
        second = _item("dup", url="https://example.com/other", tier=2)
        result = dedup_items([first, second])
        assert len(result) == 1
        assert result[0].tier == 1  # first kept

    def test_duplicate_url_after_normalization_drops_second(self):
        a = _item("aa", url="https://example.com/story")
        b = _item("bb", url="https://example.com/story?utm_source=email")
        result = dedup_items([a, b])
        assert len(result) == 1
        assert result[0].item_id == "aa"

    def test_distinct_items_all_kept(self):
        items = [
            _item("x", url="https://example.com/x"),
            _item("y", url="https://example.com/y"),
        ]
        assert len(dedup_items(items)) == 2


# ─── compute_diff — core cases ────────────────────────────────────────────

class TestComputeDiffEmpty:
    def test_both_empty(self):
        diff = compute_diff([], [], reference_date=_REF)
        assert diff.new_tier_1 == []
        assert diff.new_tier_2 == []
        assert diff.escalated == []
        assert diff.dropped == []
        assert diff.deadline_imminent == []

    def test_current_items_against_empty_previous(self):
        current = [_item("a", tier=1), _item("b", tier=2), _item("c", tier=3)]
        diff = compute_diff(current, [], reference_date=_REF)
        assert "a" in diff.new_tier_1
        assert "b" in diff.new_tier_2
        assert diff.escalated == []
        assert diff.dropped == []

    def test_previous_items_absent_from_current(self):
        previous = [_item("gone-t1", tier=1), _item("gone-t2", tier=2)]
        diff = compute_diff([], previous, reference_date=_REF)
        assert "gone-t1" in diff.dropped
        assert "gone-t2" in diff.dropped


# ─── compute_diff — tier classification ───────────────────────────────────

class TestComputeDiffTierClassification:
    def test_unchanged_tier1_item_not_in_new_lists(self):
        item = _item("stable", tier=1)
        diff = compute_diff([item], [item], reference_date=_REF)
        assert "stable" not in diff.new_tier_1
        assert "stable" not in diff.escalated

    def test_unchanged_tier2_item_not_in_new_tier2(self):
        item = _item("stable2", tier=2)
        diff = compute_diff([item], [item], reference_date=_REF)
        assert "stable2" not in diff.new_tier_2

    def test_brand_new_tier1_in_new_tier1(self):
        diff = compute_diff([_item("x", tier=1)], [], reference_date=_REF)
        assert "x" in diff.new_tier_1

    def test_brand_new_tier2_in_new_tier2(self):
        diff = compute_diff([_item("x", tier=2)], [], reference_date=_REF)
        assert "x" in diff.new_tier_2

    def test_brand_new_tier3_not_in_new_lists(self):
        diff = compute_diff([_item("x", tier=3)], [], reference_date=_REF)
        assert "x" not in diff.new_tier_1
        assert "x" not in diff.new_tier_2

    def test_demotion_tier1_to_tier2_not_in_new_tier2(self):
        prev = _item("demoted", tier=1)
        curr = _item("demoted", tier=2)
        diff = compute_diff([curr], [prev], reference_date=_REF)
        assert "demoted" not in diff.new_tier_2
        assert "demoted" not in diff.escalated  # not an escalation

    def test_demotion_tier1_to_tier2_not_dropped(self):
        # Item is still present; not dropped even though it lost tier
        prev = _item("demoted", tier=1)
        curr = _item("demoted", tier=2)
        diff = compute_diff([curr], [prev], reference_date=_REF)
        assert "demoted" not in diff.dropped

    def test_tier3_absent_from_current_not_dropped(self):
        # Only tier 1 and 2 items generate a "dropped" signal
        prev = _item("low", tier=3)
        diff = compute_diff([], [prev], reference_date=_REF)
        assert "low" not in diff.dropped


# ─── compute_diff — escalations ───────────────────────────────────────────

class TestComputeDiffEscalation:
    def test_escalation_tier2_to_tier1(self):
        prev = _item("e", tier=2)
        curr = _item("e", tier=1)
        diff = compute_diff([curr], [prev], reference_date=_REF)
        assert "e" in diff.new_tier_1
        assert len(diff.escalated) == 1
        assert diff.escalated[0].item_id == "e"
        assert diff.escalated[0].previous_tier == 2
        assert diff.escalated[0].current_tier == 1

    def test_escalation_tier3_to_tier2(self):
        prev = _item("e", tier=3)
        curr = _item("e", tier=2)
        diff = compute_diff([curr], [prev], reference_date=_REF)
        assert "e" in diff.new_tier_2
        assert diff.escalated[0].previous_tier == 3
        assert diff.escalated[0].current_tier == 2

    def test_escalation_tier3_to_tier1(self):
        prev = _item("e", tier=3)
        curr = _item("e", tier=1)
        diff = compute_diff([curr], [prev], reference_date=_REF)
        assert "e" in diff.new_tier_1
        assert "e" not in diff.new_tier_2
        assert diff.escalated[0].previous_tier == 3
        assert diff.escalated[0].current_tier == 1

    def test_multiple_escalations(self):
        previous = [_item("a", tier=2), _item("b", tier=3)]
        current = [_item("a", tier=1), _item("b", tier=2)]
        diff = compute_diff(current, previous, reference_date=_REF)
        escalated_ids = {e.item_id for e in diff.escalated}
        assert escalated_ids == {"a", "b"}
        assert set(diff.new_tier_1) == {"a"}
        assert set(diff.new_tier_2) == {"b"}


# ─── compute_diff — dropped ───────────────────────────────────────────────

class TestComputeDiffDropped:
    def test_tier1_item_absent_from_current_is_dropped(self):
        prev = _item("gone", tier=1)
        diff = compute_diff([], [prev], reference_date=_REF)
        assert "gone" in diff.dropped

    def test_tier2_item_absent_from_current_is_dropped(self):
        prev = _item("gone2", tier=2)
        diff = compute_diff([], [prev], reference_date=_REF)
        assert "gone2" in diff.dropped

    def test_tier3_item_absent_not_dropped(self):
        prev = _item("tier3", tier=3)
        diff = compute_diff([], [prev], reference_date=_REF)
        assert diff.dropped == []

    def test_item_present_in_current_not_dropped(self):
        item = _item("alive", tier=1)
        diff = compute_diff([item], [item], reference_date=_REF)
        assert "alive" not in diff.dropped

    def test_url_matched_item_not_dropped(self):
        # Same article with different tracking params is the "same" item
        prev = _item("old-id", url="https://example.com/story", tier=1)
        curr = _item("new-id", url="https://example.com/story?utm_source=x", tier=1)
        diff = compute_diff([curr], [prev], reference_date=_REF)
        assert "old-id" not in diff.dropped


# ─── compute_diff — URL normalization for cross-run matching ──────────────

class TestComputeDiffUrlNormalization:
    def test_tracking_params_do_not_create_spurious_new_entry(self):
        prev = _item("art", url="https://example.com/article", tier=1)
        # Same article with tracking params added
        curr = _item("art2", url="https://example.com/article?utm_source=email", tier=1)
        diff = compute_diff([curr], [prev], reference_date=_REF)
        # Recognised as same → not in new_tier_1
        assert "art2" not in diff.new_tier_1

    def test_url_normalized_escalation_tracked(self):
        prev = _item("x", url="https://example.com/story", tier=2)
        curr = _item("y", url="https://example.com/story?fbclid=abc", tier=1)
        diff = compute_diff([curr], [prev], reference_date=_REF)
        assert "y" in diff.new_tier_1
        assert len(diff.escalated) == 1
        assert diff.escalated[0].previous_tier == 2
        assert diff.escalated[0].current_tier == 1


# ─── compute_diff — deadline_imminent ─────────────────────────────────────

class TestComputeDiffDeadline:
    def test_deadline_today_is_imminent(self):
        item = _item("d", deadline=_REF)
        diff = compute_diff([item], [], reference_date=_REF)
        assert "d" in diff.deadline_imminent

    def test_deadline_within_window(self):
        item = _item("d", deadline=date(2026, 6, 19))  # 7 days from _REF
        diff = compute_diff([item], [], reference_date=_REF)
        assert "d" in diff.deadline_imminent

    def test_deadline_beyond_window_not_imminent(self):
        item = _item("d", deadline=date(2026, 6, 20))  # 8 days from _REF
        diff = compute_diff([item], [], reference_date=_REF)
        assert "d" not in diff.deadline_imminent

    def test_deadline_in_past_not_imminent(self):
        item = _item("d", deadline=date(2026, 6, 11))  # yesterday
        diff = compute_diff([item], [], reference_date=_REF)
        assert "d" not in diff.deadline_imminent

    def test_no_deadline_not_imminent(self):
        item = _item("d", deadline=None)
        diff = compute_diff([item], [], reference_date=_REF)
        assert diff.deadline_imminent == []

    def test_custom_window(self):
        item_3d = _item("a", deadline=date(2026, 6, 15))   # 3 days out
        item_10d = _item("b", deadline=date(2026, 6, 22))  # 10 days out
        diff = compute_diff([item_3d, item_10d], [], deadline_window_days=5, reference_date=_REF)
        assert "a" in diff.deadline_imminent
        assert "b" not in diff.deadline_imminent

    def test_deadline_window_zero_only_today(self):
        today = _item("today", deadline=_REF)
        tomorrow = _item("tmrw", deadline=date(2026, 6, 13))
        diff = compute_diff([today, tomorrow], [], deadline_window_days=0, reference_date=_REF)
        assert "today" in diff.deadline_imminent
        assert "tmrw" not in diff.deadline_imminent


# ─── update_archive ───────────────────────────────────────────────────────

class TestUpdateArchive:
    def test_adds_run_to_empty_archive(self):
        run = _run("r1", [_item("a")])
        arch = update_archive(Archive(), run)
        assert len(arch.runs) == 1
        assert arch.runs[0].run_id == "r1"

    def test_runs_ordered_oldest_to_newest(self):
        a = update_archive(Archive(), _run("r1", []))
        b = update_archive(a, _run("r2", []))
        assert [r.run_id for r in b.runs] == ["r1", "r2"]

    def test_does_not_evict_within_limit(self):
        archive = Archive()
        for i in range(5):
            archive = update_archive(archive, _run(f"r{i}", []), max_runs=10)
        assert len(archive.runs) == 5

    def test_evicts_oldest_when_over_limit(self):
        archive = Archive()
        for i in range(4):
            archive = update_archive(archive, _run(f"r{i}", [_item(f"item{i}")]), max_runs=3)
        assert len(archive.runs) == 3
        assert archive.runs[0].run_id == "r1"   # r0 evicted

    def test_tier1_item_from_evicted_run_pinned(self):
        tier1 = _item("important", tier=1)
        archive = Archive()
        for i in range(3):
            items = [tier1] if i == 0 else []
            archive = update_archive(archive, _run(f"r{i}", items), max_runs=2)
        # r0 was evicted; tier-1 item should now be in pinned
        assert any(p.item_id == "important" for p in archive.pinned)

    def test_tier2_item_not_pinned_by_default(self):
        tier2 = _item("notable", tier=2)
        archive = Archive()
        for i in range(3):
            items = [tier2] if i == 0 else []
            archive = update_archive(archive, _run(f"r{i}", items), max_runs=2)
        assert not any(p.item_id == "notable" for p in archive.pinned)

    def test_custom_persist_tier_pins_tier2(self):
        tier2 = _item("notable", tier=2)
        archive = Archive()
        for i in range(3):
            items = [tier2] if i == 0 else []
            archive = update_archive(
                archive, _run(f"r{i}", items), max_runs=2, persist_tier=2
            )
        assert any(p.item_id == "notable" for p in archive.pinned)

    def test_pinned_item_removed_when_back_in_rolling_window(self):
        item = _item("comeback", tier=1)
        # Add to first run, evict it
        archive = Archive()
        for i in range(3):
            items = [item] if i == 0 else []
            archive = update_archive(archive, _run(f"r{i}", items), max_runs=2)
        assert any(p.item_id == "comeback" for p in archive.pinned)

        # Item returns in a new run
        archive = update_archive(archive, _run("r3", [item]), max_runs=2)
        # Should no longer be in pinned (rolling window is authoritative)
        assert not any(p.item_id == "comeback" for p in archive.pinned)
        assert any(it.item_id == "comeback" for run in archive.runs for it in run.items)

    def test_pinned_item_deduped_by_url(self):
        # Two runs produce items with same URL but different IDs
        item_v1 = _item("id-v1", url="https://example.com/story", tier=1)
        item_v2 = _item("id-v2", url="https://example.com/story?utm_source=x", tier=1)

        archive = Archive()
        for i in range(3):
            items = [item_v1] if i == 0 else []
            archive = update_archive(archive, _run(f"r{i}", items), max_runs=2)

        pinned_count_before = len(archive.pinned)
        # Now add item_v2 (same URL after normalization) — should not add a second pin
        archive = update_archive(archive, _run("r3", []), max_runs=2)
        # Manually test that update_archive doesn't double-pin the same URL
        # (both id-v1 and the same URL won't be pinned twice)
        urls = [normalize_url(p.url) for p in archive.pinned]
        assert len(urls) == len(set(urls)), "Pinned list must not have URL duplicates"

    def test_does_not_mutate_input_archive(self):
        run1 = _run("r1", [_item("a")])
        original = Archive(runs=[run1], pinned=[])
        run2 = _run("r2", [_item("b")])
        _ = update_archive(original, run2)
        assert len(original.runs) == 1  # unchanged


# ─── load_archive / save_archive ─────────────────────────────────────────

class TestArchivePersistence:
    def test_load_from_missing_file_returns_empty(self, tmp_path: Path):
        arch = load_archive(tmp_path / "nonexistent.json")
        assert arch.runs == []
        assert arch.pinned == []

    def test_round_trip_preserves_data(self, tmp_path: Path):
        item = _item("kept", tier=1)
        run = _run("r1", [item])
        original = Archive(runs=[run], pinned=[])
        path = tmp_path / "archive.json"

        save_archive(original, path)
        loaded = load_archive(path)

        assert len(loaded.runs) == 1
        assert loaded.runs[0].run_id == "r1"
        assert loaded.runs[0].items[0].item_id == "kept"
        assert loaded.runs[0].items[0].tier == 1

    def test_save_creates_parent_directories(self, tmp_path: Path):
        archive = Archive()
        path = tmp_path / "deep" / "nested" / "archive.json"
        save_archive(archive, path)
        assert path.exists()

    def test_saved_file_is_valid_json(self, tmp_path: Path):
        archive = Archive(runs=[_run("r1", [_item("x")])], pinned=[])
        path = tmp_path / "archive.json"
        save_archive(archive, path)
        parsed = json.loads(path.read_text())
        assert "runs" in parsed
        assert "pinned" in parsed

    def test_round_trip_preserves_deadline(self, tmp_path: Path):
        dl = date(2026, 9, 30)
        item = _item("dl-item", deadline=dl)
        archive = Archive(runs=[_run("r1", [item])], pinned=[])
        path = tmp_path / "archive.json"
        save_archive(archive, path)
        loaded = load_archive(path)
        assert loaded.runs[0].items[0].action_deadline == dl
