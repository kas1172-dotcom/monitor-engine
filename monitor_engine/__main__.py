"""
python -m monitor_engine --config PATH --output DIR [options]

Runs the full monitor pipeline for one client config: collect, prefilter,
analyse (LLM), archive, and build a self-contained static site.

For a quick connectivity test without running the full pipeline, use the
dedicated collector test runner:

    python -m monitor_engine.collectors --config PATH --days-back 7 --max-items 3
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    ap = argparse.ArgumentParser(
        prog="python -m monitor_engine",
        description="Run the full monitor pipeline for one client config.",
    )
    ap.add_argument(
        "--config", required=True, type=Path, metavar="PATH",
        help="Path to client config JSON",
    )
    ap.add_argument(
        "--output", default=Path("output"), type=Path, metavar="DIR",
        help="Output directory for site artifacts (default: output/)",
    )
    ap.add_argument(
        "--archive", default=None, type=Path, metavar="PATH",
        help="Archive JSON path (default: <output>/archive.json)",
    )
    ap.add_argument(
        "--days-back", type=int, default=7, metavar="N",
        help="Lookback window for collectors (default: 7)",
    )
    ap.add_argument(
        "--max-items", type=int, default=50, metavar="N",
        help="Max items per source (default: 50)",
    )
    ap.add_argument(
        "--skip-analysis", action="store_true",
        help="Collect only; skip LLM analysis (useful for local testing)",
    )
    args = ap.parse_args()

    from monitor_engine.pipeline import run_pipeline

    run_pipeline(
        args.config,
        args.output,
        archive_path=args.archive,
        days_back=args.days_back,
        max_items_per_source=args.max_items,
        skip_analysis=args.skip_analysis,
    )


if __name__ == "__main__":
    main()
