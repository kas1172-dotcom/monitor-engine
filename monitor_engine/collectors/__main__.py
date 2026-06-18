"""
Test-mode runner for all configured sources.  Reads a JSON client config.

Usage:
    python -m monitor_engine.collectors --config clients/example/config.json
    python -m monitor_engine.collectors --config clients/example/config.json --days-back 14 --max-items 3
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from monitor_engine.collectors.base import check_env_vars, handler_registry, make_session
from monitor_engine.models import ClientConfig


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run each configured source and report item counts, a sample item, "
                    "and date-parse failure rate."
    )
    parser.add_argument("--config", required=True, type=Path, help="Path to client config JSON")
    parser.add_argument("--days-back", type=int, default=7, metavar="N")
    parser.add_argument("--max-items", type=int, default=5, metavar="N")
    args = parser.parse_args()

    config = ClientConfig.model_validate(json.loads(args.config.read_text()))

    try:
        check_env_vars(config)
    except EnvironmentError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        sys.exit(1)

    session = make_session()
    handler_map = handler_registry(session)

    zero: list[str] = []
    errors: list[tuple[str, str]] = []

    for source in config.sources:
        sep = "─" * 64
        print(f"\n{sep}")
        print(f"SOURCE  {source.name}  [{source.type}]  id={source.id}")
        print(f"URL     {source.url}")

        try:
            result = handler_map[source.type].collect(
                source, days_back=args.days_back, max_items=args.max_items
            )
            n = len(result.items)
            fail_pct = (
                f"{result.date_parse_failures}/{n} ({result.date_parse_failures/n*100:.0f}%)"
                if n > 0 else "n/a"
            )
            print(f"ITEMS   {n}  |  date-parse failures: {fail_pct}")
            if result.items:
                s = result.items[0]
                print(f"  title:     {s.title[:90]}")
                print(f"  url:       {s.url}")
                print(f"  published: {s.published_date or '(unknown)'}")
                print(f"  summary:   {(s.summary or '')[:120] or '(none)'}")
            else:
                zero.append(source.id)
                print("  ⚠  zero items returned")
        except Exception as exc:  # noqa: BLE001
            errors.append((source.id, str(exc)))
            print(f"  ✗  ERROR: {exc}")

    sep = "═" * 64
    print(f"\n{sep}")
    print("SUMMARY")
    print(f"  sources tested  : {len(config.sources)}")
    print(f"  OK              : {len(config.sources) - len(errors) - len(zero)}")
    if zero:
        print(f"  zero items      : {len(zero)}")
        for sid in zero:
            print(f"    ⚠ {sid}")
    if errors:
        print(f"  errors          : {len(errors)}")
        for sid, msg in errors:
            print(f"    ✗ {sid}: {msg}")

    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
