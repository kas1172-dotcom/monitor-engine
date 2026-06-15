"""
Pure functions for archive management.

No I/O except in load_archive / save_archive; everything else is data in → data out.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from monitor_engine.models import (
    AnalyzedItem,
    Archive,
    ArchivedRun,
    EscalatedItem,
    WhatsDiff,
)

DEFAULT_MAX_RUNS: int = 26
DEFAULT_PERSIST_TIER: int = 1
DEFAULT_DEADLINE_WINDOW_DAYS: int = 7

# Query parameters that carry zero content identity — stripped when normalizing URLs
_TRACKING_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "utm_reader", "utm_name",
    "fbclid", "gclid", "msclkid", "dclid",
    "mc_cid", "mc_eid",
    "_ga", "_gl",
    "ref", "source",
    "igshid",
})


# ─── URL normalization ─────────────────────────────────────────────────────

def normalize_url(url: str) -> str:
    """
    Return a canonical form of *url* suitable for cross-run de-duplication:

    - Lowercase scheme and host
    - Strip known tracking / referral query parameters
    - Sort remaining query parameters (stable comparison)
    - Remove trailing slash from the path component
    - Drop the fragment (``#…``)
    """
    if not url:
        return url
    try:
        p = urlparse(url)
    except Exception:
        return url.strip().lower()

    if p.query:
        qs = parse_qs(p.query, keep_blank_values=True)
        kept = {k: v for k, v in qs.items() if k.lower() not in _TRACKING_PARAMS}
        new_query = urlencode(sorted(kept.items()), doseq=True)
    else:
        new_query = ""

    return urlunparse((
        p.scheme.lower(),
        p.netloc.lower(),
        p.path.rstrip("/"),
        p.params,
        new_query,
        "",   # drop fragment
    ))


# ─── Within-batch de-duplication ──────────────────────────────────────────

def dedup_items(items: list[AnalyzedItem]) -> list[AnalyzedItem]:
    """
    Remove duplicate items within *items*, keeping the first occurrence.
    Two items are duplicates if they share an ``item_id`` or normalise to the same URL.
    """
    seen_ids: set[str] = set()
    seen_urls: set[str] = set()
    out: list[AnalyzedItem] = []
    for item in items:
        nurl = normalize_url(item.url)
        if item.item_id in seen_ids or nurl in seen_urls:
            continue
        seen_ids.add(item.item_id)
        seen_urls.add(nurl)
        out.append(item)
    return out


# ─── Cross-run index helpers ───────────────────────────────────────────────

def _build_index(
    items: list[AnalyzedItem],
) -> tuple[dict[str, AnalyzedItem], dict[str, AnalyzedItem]]:
    """Return ``(by_id, by_normalised_url)`` look-up dicts for *items*."""
    by_id: dict[str, AnalyzedItem] = {}
    by_url: dict[str, AnalyzedItem] = {}
    for item in items:
        by_id.setdefault(item.item_id, item)
        by_url.setdefault(normalize_url(item.url), item)
    return by_id, by_url


def _match(
    item: AnalyzedItem,
    by_id: dict[str, AnalyzedItem],
    by_url: dict[str, AnalyzedItem],
) -> AnalyzedItem | None:
    """Find a matching item from a previous run by id, then by normalised URL."""
    hit = by_id.get(item.item_id)
    if hit is not None:
        return hit
    return by_url.get(normalize_url(item.url))


# ─── What's-new diff ──────────────────────────────────────────────────────

def compute_diff(
    current: list[AnalyzedItem],
    previous: list[AnalyzedItem],
    *,
    deadline_window_days: int = DEFAULT_DEADLINE_WINDOW_DAYS,
    reference_date: date | None = None,
) -> WhatsDiff:
    """
    Compute what changed between *current* and *previous*.

    ``new_tier_1``
        Items at tier 1 that were absent or not tier 1 in *previous*.
    ``new_tier_2``
        Items at tier 2 that were absent or tier 3 in *previous*.
        Demotions from tier 1 → tier 2 are intentionally excluded.
    ``escalated``
        Items whose tier improved (numerically lower) vs *previous*.
        Overlaps with ``new_tier_1`` / ``new_tier_2`` for escalated items.
    ``dropped``
        Tier-1/2 items from *previous* that are absent from *current*
        (matched by id and normalised URL).
    ``deadline_imminent``
        Items whose ``action_deadline`` falls within
        ``[0, deadline_window_days]`` days of *reference_date*.
    """
    ref = reference_date or date.today()
    prev_by_id, prev_by_url = _build_index(previous)
    curr_ids = {it.item_id for it in current}
    curr_by_url = {normalize_url(it.url): it for it in current}

    new_tier_1: list[str] = []
    new_tier_2: list[str] = []
    escalated: list[EscalatedItem] = []
    deadline_imminent: list[str] = []

    for item in current:
        prev = _match(item, prev_by_id, prev_by_url)

        # New to tier 1: brand-new item or promoted from tier 2/3
        if item.tier == 1 and (prev is None or prev.tier != 1):
            new_tier_1.append(item.item_id)

        # New to tier 2: brand-new item or promoted from tier 3 only
        # (demotion tier 1 → tier 2 is not a "new" signal)
        if item.tier == 2 and (prev is None or prev.tier > 2):
            new_tier_2.append(item.item_id)

        # Escalated: tier number decreased (improvement)
        if prev is not None and item.tier < prev.tier:
            escalated.append(
                EscalatedItem(
                    item_id=item.item_id,
                    previous_tier=prev.tier,
                    current_tier=item.tier,
                )
            )

        # Deadline within alert window
        if item.action_deadline is not None:
            days_until = (item.action_deadline - ref).days
            if 0 <= days_until <= deadline_window_days:
                deadline_imminent.append(item.item_id)

    # Dropped: notable items from previous that are gone from current
    dropped: list[str] = [
        prev_item.item_id
        for prev_item in previous
        if prev_item.tier in (1, 2)
        and prev_item.item_id not in curr_ids
        and normalize_url(prev_item.url) not in curr_by_url
    ]

    return WhatsDiff(
        new_tier_1=new_tier_1,
        new_tier_2=new_tier_2,
        escalated=escalated,
        dropped=dropped,
        deadline_imminent=deadline_imminent,
    )


# ─── Archive maintenance ───────────────────────────────────────────────────

def update_archive(
    archive: Archive,
    new_run: ArchivedRun,
    *,
    max_runs: int = DEFAULT_MAX_RUNS,
    persist_tier: int = DEFAULT_PERSIST_TIER,
) -> Archive:
    """
    Append *new_run* and evict runs beyond *max_runs*.

    Items with ``tier <= persist_tier`` from evicted runs are moved to
    ``archive.pinned`` so they survive beyond the rolling window.  Pinned
    items that reappear inside the rolling window are removed from ``pinned``
    (the rolling-window copy is the authoritative version).

    The input archive is not mutated; a new :class:`Archive` is returned.
    """
    runs: list[ArchivedRun] = list(archive.runs) + [new_run]
    pinned: list[AnalyzedItem] = list(archive.pinned)

    # Evict oldest runs, pinning high-importance items
    while len(runs) > max_runs:
        evicted = runs.pop(0)
        pinned_ids = {p.item_id for p in pinned}
        pinned_urls = {normalize_url(p.url) for p in pinned}
        for item in evicted.items:
            if item.tier <= persist_tier:
                nurl = normalize_url(item.url)
                if item.item_id not in pinned_ids and nurl not in pinned_urls:
                    pinned.append(item)
                    pinned_ids.add(item.item_id)
                    pinned_urls.add(nurl)

    # Remove pinned entries that are already in the rolling window
    rolling_ids = {it.item_id for run in runs for it in run.items}
    rolling_urls = {normalize_url(it.url) for run in runs for it in run.items}
    pinned = [
        p for p in pinned
        if p.item_id not in rolling_ids and normalize_url(p.url) not in rolling_urls
    ]

    return Archive(runs=runs, pinned=pinned)


# ─── Persistence ──────────────────────────────────────────────────────────

def load_archive(path: Path) -> Archive:
    """Read the archive from *path*. Returns an empty :class:`Archive` if the file is absent."""
    if not path.exists():
        return Archive()
    return Archive.model_validate_json(path.read_text(encoding="utf-8"))


def save_archive(archive: Archive, path: Path) -> None:
    """Write *archive* to *path* as indented JSON. Creates parent directories as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(archive.model_dump_json(indent=2), encoding="utf-8")
