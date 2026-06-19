"""Tests for conservative same-event grouping."""
from __future__ import annotations

from datetime import datetime, timezone

from monitor_engine.analysis.grouping import group_related_items
from monitor_engine.models import AnalyzedItem, EditionAnalysis


def _item(item_id: str, title: str, *, day: int | None = 10, score: int = 50,
          source: str = "src", tier: int = 2) -> AnalyzedItem:
    published = datetime(2026, 6, day, tzinfo=timezone.utc) if day is not None else None
    return AnalyzedItem(
        item_id=item_id,
        title=title,
        url=f"https://example.com/{item_id}",
        source_id=source,
        published_at=published,
        collected_at=datetime(2026, 6, 17, tzinfo=timezone.utc),
        tier=tier,
        per_edition={"ed": EditionAnalysis(relevance_score=score, so_what="x", now_what="y", categories=[])},
    )


class TestGrouping:
    def test_similar_titles_close_dates_group(self):
        a = _item("a", "FDA Recalls Lidocaine Injection Over Contamination", day=10)
        b = _item("b", "Lidocaine Injection Recalled by FDA for Contamination", day=11)
        out = group_related_items([a, b])
        assert len(out) == 1
        assert len(out[0].also_covered_by) == 1

    def test_distinct_items_not_grouped(self):
        a = _item("a", "FDA Recalls Lidocaine Injection Over Contamination", day=10)
        b = _item("b", "Congress Passes Medicare Telehealth Extension Bill", day=10)
        out = group_related_items([a, b])
        assert len(out) == 2
        assert all(not i.also_covered_by for i in out)

    def test_one_dated_one_undated_does_not_group(self):
        a = _item("a", "FDA Recalls Lidocaine Injection Over Contamination", day=10)
        b = _item("b", "FDA Recalls Lidocaine Injection Over Contamination Issue", day=None)
        out = group_related_items([a, b])
        assert len(out) == 2  # proximity can't be confirmed → conservative no-group

    def test_dates_far_apart_do_not_group(self):
        a = _item("a", "FDA Recalls Lidocaine Injection Over Contamination", day=1)
        b = _item("b", "FDA Recalls Lidocaine Injection Over Contamination", day=20)
        out = group_related_items([a, b])
        assert len(out) == 2

    def test_both_undated_can_group(self):
        a = _item("a", "FDA Recalls Lidocaine Injection Over Contamination", day=None)
        b = _item("b", "Lidocaine Injection Recalled by FDA for Contamination", day=None)
        out = group_related_items([a, b])
        assert len(out) == 1

    def test_primary_is_highest_relevance(self):
        a = _item("a", "FDA Recalls Lidocaine Injection Over Contamination", day=10, score=40, source="wire")
        b = _item("b", "Lidocaine Injection Recalled by FDA for Contamination", day=10, score=90, source="fda")
        out = group_related_items([a, b])
        assert len(out) == 1
        assert out[0].item_id == "b"                 # higher score → primary
        assert out[0].also_covered_by[0].source_id == "wire"

    def test_three_way_cluster(self):
        items = [
            _item("a", "FDA Recalls Lidocaine Injection Over Contamination", day=10, score=30),
            _item("b", "Lidocaine Injection Recalled by FDA for Contamination", day=11, score=80),
            _item("c", "FDA Issues Recall of Lidocaine Injection for Contamination", day=12, score=50),
        ]
        out = group_related_items(items)
        assert len(out) == 1
        assert out[0].item_id == "b"
        assert {r.item_id for r in out[0].also_covered_by} == {"a", "c"}

    def test_singletons_pass_through_unchanged(self):
        items = [
            _item("a", "Congress Passes Telehealth Bill", day=10),
            _item("b", "CMS Finalizes Hospital Payment Rule", day=11),
            _item("c", "GAO Report on Veterans Health Oversight", day=12),
        ]
        out = group_related_items(items)
        assert [i.item_id for i in out] == ["a", "b", "c"]
        assert all(not i.also_covered_by for i in out)
