"""
Full pipeline: collect → prefilter → analyse → archive → site.

Entry point: monitor_engine/__main__.py
Direct use:  from monitor_engine.pipeline import run_pipeline
"""
from __future__ import annotations

import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from monitor_engine.archive import (
    compute_diff,
    dedup_items,
    load_archive,
    save_archive,
    update_archive,
)
from monitor_engine.collectors.base import collect_all
from monitor_engine.feedback import (
    apply_to_analyzed,
    apply_to_config,
    filter_muted_sources,
    load_feedback,
)
from monitor_engine.models import (
    ArchivedRun,
    ClientConfig,
    RawItem,
    RunMeta,
    RunOutput,
)
from monitor_engine.site.builder import build_site

logger = logging.getLogger(__name__)


def apply_prefilter(items: list[RawItem], include: list[str], exclude: list[str]) -> list[RawItem]:
    """
    Case-insensitive substring match on title + summary.
    Empty *include* list passes everything.  *exclude* always overrides.
    """
    def _text(item: RawItem) -> str:
        return f"{item.title} {item.summary or ''}".lower()

    lc_inc = [k.lower() for k in include]
    lc_exc = [k.lower() for k in exclude]

    out = []
    for item in items:
        t = _text(item)
        if lc_exc and any(ex in t for ex in lc_exc):
            continue
        if lc_inc and not any(inc in t for inc in lc_inc):
            continue
        out.append(item)
    return out


def _new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:4]


def _engine_version() -> str:
    try:
        from importlib.metadata import version
        return version("monitor_engine")
    except Exception:
        return "dev"


def run_pipeline(
    config_path: Path,
    output_dir: Path,
    *,
    archive_path: Path | None = None,
    days_back: int = 7,
    max_items_per_source: int = 50,
    skip_analysis: bool = False,
) -> RunOutput:
    """
    Run the full pipeline for one client config.

    Writes ``index.html`` and ``run_output.json`` to *output_dir* and updates
    the rolling archive.  Calls ``sys.exit(1)`` with a clear message if any
    required env var is missing or if the output artifact fails schema
    validation.
    """
    config = ClientConfig.model_validate(
        json.loads(config_path.read_text(encoding="utf-8"))
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    arch_path = archive_path or output_dir / "archive.json"

    # ── Client feedback ──────────────────────────────────────────────────
    # Optional clients/<name>/feedback.json deterministically adjusts this run
    # (boost/mute terms, muted sources, suppressed/pinned items). Absent = no-op.
    feedback = load_feedback(config_path.parent / "feedback.json")
    config = apply_to_config(config, feedback)   # merge boost/mute into prefilter + never_discard

    # ── Guard: Anthropic key needed for analysis ─────────────────────────
    if not skip_analysis and not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "\n[MISSING ENV VAR] ANTHROPIC_API_KEY is required for LLM analysis.\n"
            "  Set it in your environment, or pass --skip-analysis to collect only.",
            file=sys.stderr,
        )
        sys.exit(1)

    # ── Collect ──────────────────────────────────────────────────────────
    logger.info("Collecting from %d source(s)…", len(config.sources))
    try:
        collection = collect_all(
            config, days_back=days_back, max_items_per_source=max_items_per_source
        )
    except EnvironmentError as exc:
        print(f"\n[MISSING ENV VARS]\n{exc}", file=sys.stderr)
        sys.exit(1)

    raw_items = collection.items
    logger.info("Collected %d raw items", len(raw_items))

    # Drop items from any source the client muted via feedback.
    if feedback.mute_sources:
        before = len(raw_items)
        raw_items = filter_muted_sources(raw_items, feedback)
        logger.info("Feedback muted %d source(s): %d → %d items",
                    len(feedback.mute_sources), before, len(raw_items))

    # Dark-source alert: a consolidated, prominent summary of any source that
    # errored or returned nothing, so the operator is alerted rather than having
    # to read scattered log lines. (CI surfaces these further; see workflow.)
    errored = [(sid, h.error) for sid, h in collection.health.items() if h.error]
    empty = [sid for sid, h in collection.health.items() if not h.error and h.zero_results]
    if errored or empty:
        print(
            f"\n[SOURCE ALERT] {len(errored)} errored, {len(empty)} returned zero "
            f"of {len(collection.health)} sources:",
            file=sys.stderr,
        )
        for sid, err in errored:
            print(f"  ✗ {sid}: {err}", file=sys.stderr)
        for sid in empty:
            print(f"  ⚠ {sid}: zero items", file=sys.stderr)

    # ── Keyword prefilter ────────────────────────────────────────────────
    pf = config.keyword_prefilter
    filtered = apply_prefilter(raw_items, pf.include, pf.exclude)
    logger.info("Prefilter: %d → %d items", len(raw_items), len(filtered))

    # ── LLM analysis ─────────────────────────────────────────────────────
    analyzed: list = []
    editorial = None
    cost_usd = 0.0

    if not skip_analysis and filtered:
        from monitor_engine.analysis.scorer import PIPELINE_MAX_CONCURRENCY, Scorer
        # Issue LLM batches concurrently — the only thing that meaningfully cuts
        # wall time on large runs (cost is unchanged: same tokens).
        scorer = Scorer(config, max_concurrency=PIPELINE_MAX_CONCURRENCY)
        analyzed, editorial, cost_usd = scorer.analyze(filtered)
        logger.info("Analysis done: %d items, $%.4f", len(analyzed), cost_usd)
    elif skip_analysis:
        logger.info("Analysis skipped (--skip-analysis flag set)")
    else:
        logger.info("No items passed prefilter; skipping analysis")

    # ── Production guard ─────────────────────────────────────────────────
    # Items went into analysis but nothing came out: every batch was
    # quarantined or every call failed.  Publishing an empty brief would
    # silently wipe the previous site, so refuse to write anything.
    # (filtered == 0 is a quiet news week, not an error — that publishes.)
    if not skip_analysis and filtered and not analyzed:
        logger.error(
            "Analysis returned 0 items from %d prefiltered items — total "
            "analysis failure. Leaving previous site and archive untouched.",
            len(filtered),
        )
        print(
            f"\n[ANALYSIS FAILURE] {len(filtered)} items entered analysis but "
            "0 came out. Not overwriting previous artifacts.",
            file=sys.stderr,
        )
        sys.exit(1)

    # ── Group same-event items ───────────────────────────────────────────
    # Collapse items covering the same underlying story into one primary card
    # (others cited as "also covered by"). Conservative — see analysis.grouping.
    if analyzed:
        from monitor_engine.analysis.grouping import group_related_items
        before = len(analyzed)
        analyzed = group_related_items(analyzed)
        if len(analyzed) != before:
            logger.info("Grouping: %d → %d items (collapsed duplicates)", before, len(analyzed))

    # Apply item-level feedback: drop suppressed items, force pinned items to tier 1.
    if analyzed and (feedback.suppress_urls or feedback.pin_urls):
        before = len(analyzed)
        analyzed = apply_to_analyzed(analyzed, feedback)
        logger.info("Feedback: suppressed %d, pinned %d item(s)",
                    before - len(analyzed), len(feedback.pin_urls))

    # ── Archive ──────────────────────────────────────────────────────────
    archive = load_archive(arch_path)
    previous_items = archive.runs[-1].items if archive.runs else []

    deduped = dedup_items(analyzed)
    diff = compute_diff(deduped, previous_items)

    new_run = ArchivedRun(
        run_id=_new_run_id(),
        run_at=datetime.now(timezone.utc),
        items=deduped,
    )

    # ── Assemble output artifact ─────────────────────────────────────────
    run_output = RunOutput(
        meta=RunMeta(
            run_id=new_run.run_id,
            run_at=new_run.run_at,
            items_collected=len(raw_items),
            items_after_prefilter=len(filtered),
            items_analyzed=len(deduped),
            estimated_cost_usd=cost_usd if not skip_analysis else None,
            engine_version=_engine_version(),
        ),
        items=deduped,
        whats_new=diff,
        editorial=editorial,
        source_health=collection.health,
    )

    # ── Build static site ────────────────────────────────────────────────
    build_site(run_output, config, output_dir)
    logger.info("Site written to %s", output_dir)

    # ── Validate (before persisting archive — bad output must not poison history) ──
    _validate_artifact(output_dir / "run_output.json")

    # ── Persist archive ───────────────────────────────────────────────────
    updated = update_archive(archive, new_run)
    save_archive(updated, arch_path)
    logger.info(
        "Archive: %d run(s), %d pinned item(s)", len(updated.runs), len(updated.pinned)
    )

    return run_output


def _validate_artifact(path: Path) -> None:
    """
    Re-read the output JSON and parse it against RunOutput.
    Calls sys.exit(1) on any failure so GitHub Actions marks the step as failed.
    """
    try:
        RunOutput.model_validate_json(path.read_text(encoding="utf-8"))
        logger.info("Artifact validation passed: %s", path.name)
    except Exception as exc:
        print(f"\n[VALIDATION FAILED] {path}: {exc}", file=sys.stderr)
        sys.exit(1)
