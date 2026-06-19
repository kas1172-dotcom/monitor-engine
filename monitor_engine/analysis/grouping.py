"""
Conservative topic grouping.

Collapses analyzed items that cover the *same underlying event* into one primary
card, citing the rest as "also covered by" (AnalyzedItem.also_covered_by). This
is distinct from archive.dedup_items, which removes exact item_id duplicates —
here the items are different (different sources/URLs) but report the same story.

The heuristic is deliberately conservative (a false merge hides a distinct item):
two items group only when BOTH gates hold —
  1. date proximity: published within DATE_PROXIMITY_DAYS, or both undated;
  2. strong title similarity: normalized-token Jaccard ≥ JACCARD_MIN, or a
     difflib ratio ≥ RATIO_MIN.
One dated + one undated never groups (proximity can't be confirmed).
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

from monitor_engine.models import AnalyzedItem, CoverageRef

DATE_PROXIMITY_DAYS = 3
JACCARD_MIN = 0.6
RATIO_MIN = 0.75

# Short, generic words that shouldn't count toward title similarity.
_STOPWORDS = frozenset(
    "the a an of in on at to for and or with from as is are be by new over after "
    "amid into its their your our this that these those will would can could".split()
)
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokens(title: str) -> set[str]:
    """Content tokens of a title: lowercased, ≥3 chars, stopwords dropped."""
    return {
        t for t in _TOKEN_RE.findall(title.lower())
        if len(t) >= 3 and t not in _STOPWORDS
    }


def _titles_similar(a: AnalyzedItem, b: AnalyzedItem) -> bool:
    ta, tb = _tokens(a.title), _tokens(b.title)
    if ta and tb:
        jaccard = len(ta & tb) / len(ta | tb)
        if jaccard >= JACCARD_MIN:
            return True
    return SequenceMatcher(None, a.title.lower(), b.title.lower()).ratio() >= RATIO_MIN


def _dates_close(a: AnalyzedItem, b: AnalyzedItem) -> bool:
    if a.published_at is None and b.published_at is None:
        return True
    if a.published_at is None or b.published_at is None:
        return False   # can't confirm proximity → don't group
    return abs((a.published_at - b.published_at).days) <= DATE_PROXIMITY_DAYS


def _same_event(a: AnalyzedItem, b: AnalyzedItem) -> bool:
    """Both gates must hold — conservative by construction."""
    return _dates_close(a, b) and _titles_similar(a, b)


def _primary_key(item: AnalyzedItem) -> tuple:
    """Sort key for choosing the primary card within a cluster (max wins):
    most relevant first, then earliest published (broke the story), then a
    stable id tiebreak. Absent a configured source-authority ranking, relevance
    stands in for authority."""
    published_ordinal = item.published_at.timestamp() if item.published_at else float("inf")
    return (item.importance_score, -published_ordinal, item.item_id)


def group_related_items(items: list[AnalyzedItem]) -> list[AnalyzedItem]:
    """Collapse same-event items into primaries carrying also_covered_by refs.

    Order-stable: clusters appear in first-seen order; non-duplicate items pass
    through unchanged. Returns a new list; inputs are not mutated.
    """
    clusters: list[list[AnalyzedItem]] = []
    for item in items:
        for cluster in clusters:
            if _same_event(item, cluster[0]):
                cluster.append(item)
                break
        else:
            clusters.append([item])

    out: list[AnalyzedItem] = []
    for cluster in clusters:
        if len(cluster) == 1:
            out.append(cluster[0])
            continue
        primary = max(cluster, key=_primary_key)
        refs = [
            CoverageRef(item_id=s.item_id, source_id=s.source_id, title=s.title, url=s.url)
            for s in cluster
            if s.item_id != primary.item_id
        ]
        out.append(primary.model_copy(update={"also_covered_by": primary.also_covered_by + refs}))
    return out
