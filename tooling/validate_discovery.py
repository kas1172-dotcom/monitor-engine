"""
Live-run validation for the source-discovery agent.

Scaffolds a client's intake into briefs, runs the discovery agent against the
real network + Anthropic API, and reports how the agent's discovered sources
compare to the hand-built config for the same client (coverage by domain).

This makes live HTTP and Anthropic calls and costs API spend, so it is meant to
run on manual CI dispatch (ANTHROPIC_API_KEY from secrets), not in the test
suite. The comparison logic is factored out as a pure function and unit-tested.

Usage:
    python -m tooling.validate_discovery clients/healthcare-regulatory
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from monitor_engine.models import ClientConfig
from tooling.discovery_agent import DiscoveryAgent
from tooling.scaffold import scaffold


def _domain(url: str) -> str:
    """Bare host of a URL, lowercased with a leading 'www.' stripped."""
    netloc = urlparse(url).netloc.lower()
    return netloc[4:] if netloc.startswith("www.") else netloc


def summarize_coverage(
    discovered: list[dict[str, Any]],
    reference: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compare discovered sources against a reference (hand-built) set by domain.

    Pure and deterministic — no network. Coverage is measured by how many of the
    reference domains the agent also reached, since the agent may legitimately
    pick different-but-valid sources, so exact config equality is the wrong test.
    """
    disc_domains = {_domain(s.get("url", "")) for s in discovered} - {""}
    ref_domains = {_domain(s.get("url", "")) for s in reference} - {""}
    shared = disc_domains & ref_domains

    types: dict[str, int] = {}
    for s in discovered:
        types[s.get("type", "?")] = types.get(s.get("type", "?"), 0) + 1

    return {
        "discovered_count": len(discovered),
        "reference_count": len(reference),
        "discovered_types": types,
        "discovered_domains": sorted(disc_domains),
        "reference_domains": sorted(ref_domains),
        "shared_domains": sorted(shared),
        "missed_domains": sorted(ref_domains - disc_domains),
        "coverage_pct": round(100 * len(shared) / len(ref_domains), 1) if ref_domains else None,
    }


def _render_report(client: str, summary: dict[str, Any], result_meta: dict[str, Any]) -> str:
    lines = [
        f"### Discovery validation — {client}",
        "",
        f"- briefs resolved → **{summary['discovered_count']}** sources "
        f"({summary['discovered_types']})",
        f"- hand-built reference: **{summary['reference_count']}** sources",
    ]
    if summary["coverage_pct"] is not None:
        lines.append(
            f"- domain coverage vs. reference: **{summary['coverage_pct']}%** "
            f"({len(summary['shared_domains'])}/{summary['reference_count']})"
        )
    if summary["missed_domains"]:
        lines.append(f"- reference domains the agent did not reach: "
                     f"{', '.join(summary['missed_domains'])}")
    lines.append(
        f"- agent: {result_meta['turns']} turns, "
        f"{result_meta['input_tokens']:,} in / {result_meta['output_tokens']:,} out → "
        f"${result_meta['estimated_usd']:.4f}"
        + ("  ⚠ hit turn ceiling" if result_meta["stopped_early"] else "")
    )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("client_dir", type=Path,
                        help="Client directory containing intake.json (and config.json for comparison)")
    args = parser.parse_args()

    intake = json.loads((args.client_dir / "intake.json").read_text(encoding="utf-8"))
    scaffolded = scaffold(intake)

    result = DiscoveryAgent().discover(scaffolded["source_briefs"])

    # Inject and validate the finished config — a hard failure if it doesn't hold.
    config = scaffolded["config"]
    config["sources"] = result.sources
    ClientConfig.model_validate(config)

    # Compare against the hand-built config if one exists (informational).
    ref_path = args.client_dir / "config.json"
    reference = (
        json.loads(ref_path.read_text(encoding="utf-8")).get("sources", [])
        if ref_path.exists() else []
    )
    summary = summarize_coverage(result.sources, reference)
    meta = {
        "turns": result.turns,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "estimated_usd": result.estimated_usd,
        "stopped_early": result.stopped_early,
    }

    report = _render_report(args.client_dir.name, summary, meta)
    if step_summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        Path(step_summary).write_text(report, encoding="utf-8")
    print(report, file=sys.stderr)
    print(json.dumps(config, indent=2, ensure_ascii=False))

    # Hard gate: the run is a failure only if the agent produced nothing usable.
    if not result.sources:
        print("[FAIL] discovery produced zero sources", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
