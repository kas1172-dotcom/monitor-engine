"""
Client feedback ingestion.

Reads a committed ``feedback.json`` and applies it as deterministic adjustments
to one run — the in-architecture way to "incorporate client feedback" without a
backend, database, or learning loop. Feedback becomes config the next run obeys:

  mute_terms     → keyword_prefilter.exclude   (drop matching items pre-analysis)
  boost_terms    → keyword_prefilter.include + scoring_rubric.never_discard
                                                (force matching items to survive)
  mute_sources   → drop items from those sources before analysis
  suppress_urls  → drop those specific items from the final brief
  pin_urls       → force those specific items to tier 1

Everything here is pure and order-preserving; nothing is mutated in place.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from monitor_engine.archive.core import normalize_url
from monitor_engine.models import AnalyzedItem, ClientConfig, Feedback, RawItem

logger = logging.getLogger(__name__)


def load_feedback(path: Path) -> Feedback:
    """Load a client's feedback file, or an empty Feedback if it is absent.
    A malformed file is logged and treated as empty rather than failing the run —
    feedback must never be able to break the pipeline."""
    if not path.exists():
        return Feedback()
    try:
        return Feedback.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.error("Ignoring invalid feedback file %s: %s", path, exc)
        return Feedback()


def apply_to_config(config: ClientConfig, fb: Feedback) -> ClientConfig:
    """Return a config copy with boost/mute terms merged into the prefilter and
    never-discard list. Order-preserving and de-duplicated. Other fields unchanged."""
    if not (fb.boost_terms or fb.mute_terms):
        return config

    def _merge(existing: list[str], extra: list[str]) -> list[str]:
        seen = dict.fromkeys(existing)
        for e in extra:
            seen.setdefault(e, None)
        return list(seen)

    prefilter = config.keyword_prefilter.model_copy(update={
        "include": _merge(config.keyword_prefilter.include, fb.boost_terms),
        "exclude": _merge(config.keyword_prefilter.exclude, fb.mute_terms),
    })
    rubric = config.scoring_rubric.model_copy(update={
        "never_discard": _merge(config.scoring_rubric.never_discard, fb.boost_terms),
    })
    return config.model_copy(update={"keyword_prefilter": prefilter, "scoring_rubric": rubric})


def filter_muted_sources(items: list[RawItem], fb: Feedback) -> list[RawItem]:
    """Drop raw items from muted sources (matched by the source name shown on cards)."""
    if not fb.mute_sources:
        return items
    muted = set(fb.mute_sources)
    return [it for it in items if it.source_name not in muted]


def apply_to_analyzed(items: list[AnalyzedItem], fb: Feedback) -> list[AnalyzedItem]:
    """Drop suppressed items and force pinned items to tier 1. URL matching is
    normalized so trailing slashes / query noise don't cause misses."""
    if not (fb.suppress_urls or fb.pin_urls):
        return items
    suppressed = {normalize_url(u) for u in fb.suppress_urls}
    pinned = {normalize_url(u) for u in fb.pin_urls}

    out: list[AnalyzedItem] = []
    for item in items:
        nurl = normalize_url(item.url)
        if nurl in suppressed:
            continue
        if nurl in pinned and item.tier != 1:
            item = item.model_copy(update={"tier": 1})
        out.append(item)
    return out
