"""
Deterministic tools for the source-discovery agent.

The agent (an Anthropic tool-use loop, run in CI) reasons over a client's
plain-language "coverage" briefs and proposes candidate source configs. These
two tools are its hands and its ground truth — no LLM, no API key, fully
testable:

  fetch_sample(url)    — fetch a URL and return a truncated body for the agent to
                         read (so it can infer RSS vs JSON vs HTML, item paths,
                         field names, selectors).
  probe_source(source) — THE ORACLE: validate a proposed source against the real
                         Source schema, run it through the actual collector, and
                         report what came back. The agent keeps a source only if
                         this returns ok=True.

This makes the agent self-grading against live HTTP rather than its own
confidence — the same loop we ran by hand to build the healthcare sources.
"""
from __future__ import annotations

from typing import Any

import requests
from pydantic import TypeAdapter

from monitor_engine.collectors.base import handler_registry, make_session
from monitor_engine.models import Source

_SOURCE_ADAPTER = TypeAdapter(Source)


def fetch_sample(
    url: str,
    *,
    session: requests.Session | None = None,
    user_agent: str | None = None,
    max_chars: int = 4000,
    timeout: int = 20,
) -> dict[str, Any]:
    """Fetch *url* and return a truncated body sample for the agent to inspect.
    Never raises — network/HTTP errors are returned in the dict."""
    session = session or make_session()
    headers = {"User-Agent": user_agent} if user_agent else {}
    try:
        resp = session.get(url, headers=headers, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "url": url}
    body = resp.text or ""
    return {
        "ok": resp.status_code < 400,
        "status": resp.status_code,
        "content_type": resp.headers.get("content-type", ""),
        "final_url": str(resp.url),
        "body_sample": body[:max_chars],
        "truncated": len(body) > max_chars,
    }


def probe_source(
    source: dict[str, Any],
    *,
    session: requests.Session | None = None,
    days_back: int = 365,
    max_items: int = 3,
) -> dict[str, Any]:
    """Validate *source* against the Source schema and run it through the real
    collector. Returns {ok, items_collected, date_parse_failures, sample, error}.
    ok=True means the source produced at least one item — the agent's keep/drop
    signal. Never raises."""
    try:
        validated = _SOURCE_ADAPTER.validate_python(source)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"invalid source config: {exc}"}

    session = session or make_session()
    handler = handler_registry(session)[validated.type]
    try:
        result = handler.collect(validated, days_back=days_back, max_items=max_items)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}

    items = result.items
    sample = None
    if items:
        s = items[0]
        sample = {
            "title": s.title,
            "url": s.url,
            "published": str(s.published_date) if s.published_date else None,
        }
    return {
        "ok": bool(items),
        "items_collected": len(items),
        "date_parse_failures": result.date_parse_failures,
        "sample": sample,
        "error": None if items else "zero items returned",
    }
