"""
Source-discovery agent.

Turns a client's plain-language source *briefs* (produced by tooling.scaffold)
into validated ``sources[]`` by running an Anthropic tool-use loop over the two
deterministic discovery tools:

    fetch_sample  — let the model inspect a candidate URL's body.
    probe_source  — THE ORACLE: validate a proposed config against the real
                    Source schema and run it through the actual collector.

The loop is self-grading: a proposed source is *accepted only when probe_source
returns ok=True*. We capture the source at that moment — the exact config that
passed the oracle — so the returned sources[] are correct by construction rather
than re-serialized from model free-text. This is the same hand-run loop used to
build sources by hand, now automated.

Build-time client onboarding only — this never runs in the pipeline and lives in
tooling/, not the engine package.

Usage (library):
    agent = DiscoveryAgent()            # reads ANTHROPIC_API_KEY from env
    result = agent.discover(source_briefs)
    result.sources                      # list[dict], each validates against Source

CLI (fills a scaffold result's empty sources[] and prints the finished config):
    python -m tooling.discovery_agent path/to/scaffold_output.json
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anthropic
import requests
from pydantic import TypeAdapter

from monitor_engine.collectors.base import make_session
from monitor_engine.models import ClientConfig, Source
from tooling.discovery_tools import fetch_sample, probe_source

logger = logging.getLogger(__name__)

DISCOVERY_MODEL = "claude-sonnet-4-6"
DEFAULT_MAX_TURNS = 40          # one API round-trip per turn; a safety ceiling, not a target
MAX_TOKENS = 4096

# Per-token pricing for the discovery model (input, output).
_PRICE: tuple[float, float] = (3.00e-6, 15.00e-6)

# The Source contract, derived from the single schema source of truth so the
# prompt never drifts from models.py.
_SOURCE_SCHEMA = TypeAdapter(Source).json_schema()

_TOOLS: list[dict[str, Any]] = [
    {
        "name": "fetch_sample",
        "description": (
            "Fetch a URL and return a truncated body sample so you can infer "
            "whether it is RSS, a JSON API, or an HTML list, and identify item "
            "paths, field names, or CSS selectors. Never raises."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to fetch."},
                "user_agent": {
                    "type": "string",
                    "description": "Optional User-Agent override if the default is blocked.",
                },
            },
            "required": ["url"],
        },
    },
    {
        "name": "probe_source",
        "description": (
            "Validate a candidate source config against the real Source schema "
            "and run it through the actual collector. Returns ok=true only when "
            "the source produced at least one item. This is the keep/drop oracle: "
            "a source is accepted only when this returns ok=true. On failure the "
            "error explains what to fix; revise and probe again."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "source": {
                    "type": "object",
                    "description": "A candidate Source config matching the schema in the system prompt.",
                },
            },
            "required": ["source"],
        },
    },
]


def _system_prompt() -> str:
    return (
        "You configure data sources for an automated monitoring engine. Given a "
        "list of plain-language coverage briefs, produce a working source config "
        "for each one.\n\n"
        "A source is one of three handler types and must validate against this "
        "JSON Schema:\n\n"
        f"{json.dumps(_SOURCE_SCHEMA, indent=2)}\n\n"
        "Workflow for each brief:\n"
        "1. Use fetch_sample on the candidate URL (and any URLs you find in it) to "
        "see the real response shape.\n"
        "2. Propose a source config of the most appropriate type and validate it "
        "with probe_source.\n"
        "3. If probe_source returns ok=false, read the error, revise, and probe "
        "again. A source is accepted ONLY when probe_source returns ok=true.\n\n"
        "Rules:\n"
        "- Only the three types above are supported; never invent fields outside "
        "the schema.\n"
        "- Give each source a short kebab-case id and a human-readable name.\n"
        "- Prefer RSS or JSON APIs over HTML scraping when both are available.\n"
        "- When every brief is resolved (or you have made a genuine effort and "
        "cannot make one work), stop and briefly summarize. Do not call any more "
        "tools once you are done."
    )


def _user_prompt(source_briefs: list[dict[str, Any]]) -> str:
    return (
        "Find and validate a working source for each of these coverage briefs:\n\n"
        f"{json.dumps(source_briefs, indent=2)}"
    )


@dataclass
class DiscoveryResult:
    """Outcome of a discovery run."""
    sources: list[dict[str, Any]]            # each validates against Source; oracle-passing
    turns: int                               # API round-trips used
    input_tokens: int = 0
    output_tokens: int = 0
    stopped_early: bool = False              # True if the turn ceiling was hit

    @property
    def estimated_usd(self) -> float:
        in_rate, out_rate = _PRICE
        return self.input_tokens * in_rate + self.output_tokens * out_rate


@dataclass
class _ToolUse:
    """A normalized view of one tool_use block (so we don't depend on SDK types)."""
    id: str
    name: str
    input: dict[str, Any]


class DiscoveryAgent:
    """
    Drive an Anthropic tool-use loop that resolves source briefs into validated
    source configs.

    The Anthropic client and HTTP session are injectable so the loop is testable
    without real API calls or network access.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        client: anthropic.Anthropic | None = None,
        session: requests.Session | None = None,
        model: str = DISCOVERY_MODEL,
        max_turns: int = DEFAULT_MAX_TURNS,
    ) -> None:
        self._client = client or anthropic.Anthropic(api_key=api_key)
        self._session = session or make_session()
        self._model = model
        self._max_turns = max_turns

    # ── tool dispatch ───────────────────────────────────────────────────────

    def _dispatch(self, tool: _ToolUse) -> dict[str, Any]:
        """Run one tool call against the deterministic discovery tools. Both tools
        never raise, so the model always gets a readable result to react to."""
        if tool.name == "fetch_sample":
            return fetch_sample(
                tool.input["url"],
                session=self._session,
                user_agent=tool.input.get("user_agent"),
            )
        if tool.name == "probe_source":
            return probe_source(tool.input["source"], session=self._session)
        return {"ok": False, "error": f"unknown tool: {tool.name}"}

    @staticmethod
    def _tool_uses(content: Any) -> list[_ToolUse]:
        return [
            _ToolUse(id=b.id, name=b.name, input=dict(b.input))
            for b in content
            if getattr(b, "type", None) == "tool_use"
        ]

    # ── the loop ────────────────────────────────────────────────────────────

    def discover(self, source_briefs: list[dict[str, Any]]) -> DiscoveryResult:
        """Resolve *source_briefs* into validated source configs.

        Returns the configs that passed the oracle (ok=True), de-duplicated by id
        with the last passing version winning. Always terminates: the loop stops on
        the model's end_turn or after max_turns round-trips, whichever comes first.
        """
        messages: list[dict[str, Any]] = [{"role": "user", "content": _user_prompt(source_briefs)}]
        accepted: dict[str, dict[str, Any]] = {}
        input_tokens = output_tokens = 0
        turns = 0
        stopped_early = True

        while turns < self._max_turns:
            turns += 1
            response = self._client.messages.create(
                model=self._model,
                max_tokens=MAX_TOKENS,
                system=_system_prompt(),
                tools=_TOOLS,
                messages=messages,
            )
            usage = response.usage
            input_tokens += getattr(usage, "input_tokens", 0)
            output_tokens += getattr(usage, "output_tokens", 0)

            if response.stop_reason != "tool_use":
                stopped_early = False
                break

            # Echo the assistant turn back verbatim, then answer every tool call.
            messages.append({"role": "assistant", "content": response.content})
            tool_results: list[dict[str, Any]] = []
            for tool in self._tool_uses(response.content):
                result = self._dispatch(tool)
                # Capture at the oracle: a source the oracle blessed is kept exactly
                # as the model submitted it — no re-serialization, no drift.
                if tool.name == "probe_source" and result.get("ok"):
                    src = tool.input["source"]
                    accepted[src.get("id", f"_anon_{len(accepted)}")] = src
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool.id,
                    "content": json.dumps(result),
                })
            messages.append({"role": "user", "content": tool_results})

        if stopped_early:
            logger.warning(
                "Discovery hit the %d-turn ceiling with %d source(s) accepted; "
                "stopping.", self._max_turns, len(accepted),
            )

        return DiscoveryResult(
            sources=list(accepted.values()),
            turns=turns,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            stopped_early=stopped_early,
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Resolve a scaffold result's source briefs into validated "
                    "sources[] and print the finished client config.",
    )
    parser.add_argument(
        "scaffold_result",
        type=Path,
        help="JSON file with {'config': <draft>, 'source_briefs': [...]} from tooling.scaffold",
    )
    args = parser.parse_args()

    data = json.loads(args.scaffold_result.read_text(encoding="utf-8"))
    config = data["config"]
    briefs = data.get("source_briefs", [])

    result = DiscoveryAgent().discover(briefs)
    config["sources"] = result.sources
    ClientConfig.model_validate(config)   # fail loudly if discovery produced a bad config

    print(
        f"\n── Discovery ───────────────────────────────────────\n"
        f"  briefs: {len(briefs)}  →  sources accepted: {len(result.sources)}\n"
        f"  turns: {result.turns}  |  "
        f"{result.input_tokens:,} in / {result.output_tokens:,} out  → "
        f"${result.estimated_usd:.4f}\n"
        f"────────────────────────────────────────────────────",
        file=sys.stderr,
    )
    print(json.dumps(config, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
