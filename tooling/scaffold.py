"""
Deterministic intake → draft-config scaffolder.

Turns a client's structured questionnaire answers (intake.json) into the
*deterministic* parts of a ClientConfig — branding, editions, categories,
never-discard terms, keyword prefilter, cadence, deep-analysis sections, and
cost caps — and leaves `sources` empty.

The one part that is NOT deterministic — turning the client's plain-language
"what I read / who matters" answers into working `sources[]` (real feed URLs,
JSON paths, CSS selectors) — is returned separately as `source_briefs`, to be
resolved by the source-discovery agent against the live connectivity oracle.
This module performs no network calls and needs no API key; it is the testable
glue around that agent.

Usage:
    from tooling.scaffold import scaffold
    result = scaffold(intake_dict)
    result["config"]         # dict, validates against ClientConfig (sources == [])
    result["source_briefs"]  # list of {name, what, kind, url_hint} for the agent

CLI:
    python -m tooling.scaffold path/to/intake.json
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

from monitor_engine.models import ClientConfig

DEFAULT_ACCENT = "#0F766E"
DEFAULT_THRESHOLDS = {"tier_1_min": 80, "tier_2_min": 50, "tier_3_min": 20}
DEFAULT_COST_CAPS = {"max_items_per_run": 120, "max_output_tokens_per_run": 90000}
DEFAULT_DEEP_BATCH_SIZE = 3

# Generic deep-analysis section library. Keyed by section id; the client picks
# which sections they want and we attach standard, industry-agnostic instructions.
_DEEP_SECTIONS: dict[str, dict[str, str]] = {
    "background": {
        "label": "Background", "kind": "text",
        "instruction": "Two to three sentences situating this development and what preceded it.",
    },
    "key_stakeholders": {
        "label": "Key stakeholders", "kind": "list",
        "instruction": "The organizations, agencies, or people with a material interest; "
                       "name them only if supported by the item.",
    },
    "scenarios": {
        "label": "Scenarios to watch", "kind": "list",
        "instruction": "Two or three plausible ways this could develop over the next "
                       "6–18 months, each phrased as a short conditional.",
    },
    "recommended_actions": {
        "label": "Recommended actions", "kind": "list",
        "instruction": "Concrete next steps the audience should consider this week.",
    },
}

_DEFAULT_DEEP_INSTRUCTION = (
    "Produce a concise but substantive briefing for this monitor's audience. "
    "Stay grounded in the item; flag uncertainty rather than speculating."
)

# Day name → cron day-of-week.
_CRON_DOW = {
    "sunday": "0", "monday": "1", "tuesday": "2", "wednesday": "3",
    "thursday": "4", "friday": "5", "saturday": "6",
}


def slugify(label: str) -> str:
    """Lowercase, hyphenated, alnum id derived from a label."""
    s = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")
    return s or "edition"


def cadence_to_cron(cadence: dict[str, Any]) -> str:
    """Map a canonical cadence choice to a 5-field cron expression.

    frequency: "weekly" | "weekdays" | "daily"; day (for weekly); hour_local (0–23).
    Note: the hour is the client's local hour; the actual CI cron is UTC, so the
    operator/agent aligns the workflow schedule separately — this value is the
    informational cadence echoed into the config.
    """
    hour = int(cadence.get("hour_local", 7))
    freq = cadence.get("frequency", "weekly")
    if freq == "daily":
        dow = "*"
    elif freq == "weekdays":
        dow = "1-5"
    else:  # weekly
        dow = _CRON_DOW.get(str(cadence.get("day", "monday")).lower(), "1")
    return f"0 {hour} * * {dow}"


def _editions(intake: dict[str, Any]) -> list[dict[str, Any]]:
    editions = []
    for aud in intake.get("audiences", []):
        editions.append({
            "id": slugify(aud["label"]),
            "label": aud["label"],
            "audience_description": aud.get("role", ""),
            "analysis_instructions": aud.get("matters", ""),
            "categories": list(aud.get("topics", [])),
        })
    return editions


def _prefilter_include(intake: dict[str, Any]) -> list[str]:
    """Derive prefilter include terms from each audience's topics, the named
    entities the client said matter, and any structured profile entities (so the
    profile a client fills out also widens what gets collected). De-duplicated,
    order-preserving. The agent refines this; this is a sensible draft."""
    seen: dict[str, None] = {}
    for aud in intake.get("audiences", []):
        for t in aud.get("topics", []):
            seen.setdefault(t, None)
    for ent in intake.get("named_entities", []):
        seen.setdefault(ent, None)
    ne = (intake.get("profile") or {}).get("named_entities") or {}
    for group in ("customers", "competitors", "agencies", "programs"):
        for ent in ne.get(group, []):
            seen.setdefault(ent, None)
    return list(seen)


def _deep_analysis(intake: dict[str, Any]) -> dict[str, Any]:
    sections = []
    for sid in intake.get("depth_sections", list(_DEEP_SECTIONS)):
        spec = _DEEP_SECTIONS.get(sid)
        if spec:
            sections.append({"id": sid, **spec})
    return {
        "instruction": _DEFAULT_DEEP_INSTRUCTION,
        "sections": sections,
        "deep_batch_size": DEFAULT_DEEP_BATCH_SIZE,
    }


def scaffold(intake: dict[str, Any]) -> dict[str, Any]:
    """Build {"config": <draft ClientConfig dict, sources=[]>, "source_briefs": [...]}.

    The returned config validates against ClientConfig. `source_briefs` carries the
    client's plain-language coverage answers (plus any paid-source flags) for the
    source-discovery agent to turn into real `sources[]`.
    """
    config: dict[str, Any] = {
        "branding": {
            "name": intake["monitor_name"],
            "accent_color": intake.get("accent_color") or DEFAULT_ACCENT,
        },
        "editions": _editions(intake),
        "scoring_rubric": {
            "thresholds": dict(DEFAULT_THRESHOLDS),
            "never_discard": list(intake.get("must_not_miss", [])),
        },
        "sources": [],   # filled by the source-discovery agent
        "keyword_prefilter": {
            "include": _prefilter_include(intake),
            "exclude": list(intake.get("noise_to_exclude", [])),
        },
        "deep_analysis": _deep_analysis(intake),
        "cadence": {
            "cron": cadence_to_cron(intake.get("cadence", {})),
            "timezone": intake.get("cadence", {}).get("timezone", "UTC"),
        },
        "cost_caps": dict(DEFAULT_COST_CAPS),
    }

    # Profile is consumed by the analysis prompt to make each item client-specific.
    # Pass it through when the intake carries one; ClientConfig validates the shape.
    if intake.get("profile"):
        config["profile"] = intake["profile"]

    # Validate the deterministic draft against the single source of truth.
    ClientConfig.model_validate(config)

    source_briefs = list(intake.get("coverage", []))
    for paid in intake.get("paid_sources", []):
        source_briefs.append({
            "name": paid.get("name", ""),
            "what": "Authenticated/paid source",
            "kind": "database",
            "url_hint": None,
            "auth_env_var": paid.get("env_var"),
        })

    return {"config": config, "source_briefs": source_briefs}


def main() -> None:
    if len(sys.argv) != 2:
        print("usage: python -m tooling.scaffold path/to/intake.json", file=sys.stderr)
        sys.exit(2)
    intake = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    result = scaffold(intake)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
