"""Tests for the source-discovery agent's tool-use loop.

The Anthropic client and HTTP session are both injected, so these run with no
network and no API key: scripted model responses drive the loop, and a mock
session feeds the real probe_source oracle from fixtures.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from tooling.discovery_agent import DiscoveryAgent

FIXTURES = Path(__file__).parent / "fixtures"
_RSS_BYTES = (FIXTURES / "sample_rss.xml").read_bytes()

_VALID_RSS = {"type": "rss", "id": "f", "name": "Feed", "url": "https://example.com/feed"}


# ─── builders for scripted model responses ──────────────────────────────────

def _tool_use(name: str, tool_input: dict, *, tid: str = "tu_1") -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=tid, name=name, input=tool_input)


def _text(text: str = "Done.") -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def _response(content: list, stop_reason: str, *, in_tok: int = 100, out_tok: int = 50) -> SimpleNamespace:
    return SimpleNamespace(
        content=content,
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=in_tok, output_tokens=out_tok),
    )


def _client(*responses: SimpleNamespace) -> MagicMock:
    c = MagicMock()
    c.messages.create.side_effect = list(responses)
    return c


def _looping_client(response_factory) -> MagicMock:
    """A client that returns a fresh response on every call (for ceiling tests)."""
    c = MagicMock()
    c.messages.create.side_effect = lambda **_: response_factory()
    return c


def _session(*, content: bytes = b"", text: str = "", status: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.content = content
    resp.text = text or content.decode(errors="ignore")
    resp.status_code = status
    resp.url = "https://example.com/feed"
    resp.headers = {"content-type": "application/rss+xml"}
    resp.raise_for_status.return_value = None
    s = MagicMock()
    s.get.return_value = resp
    return s


def _agent(client: MagicMock, session: MagicMock | None = None, **kw) -> DiscoveryAgent:
    return DiscoveryAgent(client=client, session=session or _session(content=_RSS_BYTES), **kw)


# ─── happy path ─────────────────────────────────────────────────────────────

class TestAcceptance:
    def test_oracle_pass_is_accepted(self):
        client = _client(
            _response([_tool_use("probe_source", {"source": _VALID_RSS})], "tool_use"),
            _response([_text()], "end_turn"),
        )
        result = _agent(client).discover([{"name": "Feed", "what": "news"}])
        assert result.sources == [_VALID_RSS]
        assert result.turns == 2
        assert result.stopped_early is False

    def test_fetch_then_probe(self):
        client = _client(
            _response([_tool_use("fetch_sample", {"url": "https://example.com/feed"})], "tool_use"),
            _response([_tool_use("probe_source", {"source": _VALID_RSS})], "tool_use"),
            _response([_text()], "end_turn"),
        )
        session = _session(content=_RSS_BYTES)
        result = _agent(client, session).discover([{"name": "Feed", "what": "news"}])
        assert result.sources == [_VALID_RSS]
        assert result.turns == 3
        assert session.get.called  # fetch_sample actually hit the (mock) network

    def test_model_ends_immediately(self):
        client = _client(_response([_text("Nothing to do.")], "end_turn"))
        result = _agent(client).discover([])
        assert result.sources == []
        assert result.turns == 1
        assert result.stopped_early is False


# ─── rejection / oracle gating ──────────────────────────────────────────────

class TestRejection:
    def test_invalid_source_not_accepted(self):
        client = _client(
            _response([_tool_use("probe_source", {"source": {"type": "rss", "id": "x"}})], "tool_use"),
            _response([_text()], "end_turn"),
        )
        result = _agent(client).discover([{"name": "Bad", "what": "x"}])
        assert result.sources == []

    def test_zero_item_source_not_accepted(self):
        # Valid config shape, but the oracle gets an empty body → zero items → ok=False.
        client = _client(
            _response([_tool_use("probe_source", {"source": _VALID_RSS})], "tool_use"),
            _response([_text()], "end_turn"),
        )
        empty = _session(content=b"<rss><channel></channel></rss>")
        result = _agent(client, empty).discover([{"name": "Feed", "what": "x"}])
        assert result.sources == []


# ─── loop mechanics ─────────────────────────────────────────────────────────

class TestLoopMechanics:
    def test_dedupes_by_id_last_wins(self):
        v1 = {**_VALID_RSS, "name": "First"}
        v2 = {**_VALID_RSS, "name": "Second"}
        client = _client(
            _response([_tool_use("probe_source", {"source": v1}, tid="a")], "tool_use"),
            _response([_tool_use("probe_source", {"source": v2}, tid="b")], "tool_use"),
            _response([_text()], "end_turn"),
        )
        result = _agent(client).discover([{"name": "Feed", "what": "x"}])
        assert len(result.sources) == 1
        assert result.sources[0]["name"] == "Second"

    def test_unknown_tool_is_reported_not_raised(self):
        client = _client(
            _response([_tool_use("teleport", {"x": 1})], "tool_use"),
            _response([_text()], "end_turn"),
        )
        result = _agent(client).discover([{"name": "x", "what": "y"}])
        # No crash, no source accepted; the model gets an error tool_result.
        assert result.sources == []
        # Second user message carries the error back to the model.
        msgs = client.messages.create.call_args_list[1].kwargs["messages"]
        tool_result = msgs[-1]["content"][0]
        assert "unknown tool" in json.loads(tool_result["content"])["error"]

    def test_stops_at_max_turns(self):
        # Model never stops calling tools; ceiling must end the loop.
        client = _looping_client(
            lambda: _response([_tool_use("fetch_sample", {"url": "https://e.com"})], "tool_use")
        )
        result = _agent(client, max_turns=3).discover([{"name": "x", "what": "y"}])
        assert result.turns == 3
        assert result.stopped_early is True
        assert result.sources == []


# ─── cost accounting ────────────────────────────────────────────────────────

class TestCost:
    def test_tokens_accumulate_and_cost_estimates(self):
        client = _client(
            _response([_tool_use("probe_source", {"source": _VALID_RSS})], "tool_use", in_tok=200, out_tok=80),
            _response([_text()], "end_turn", in_tok=300, out_tok=20),
        )
        result = _agent(client).discover([{"name": "Feed", "what": "x"}])
        assert result.input_tokens == 500
        assert result.output_tokens == 100
        # 500*3e-6 + 100*15e-6 = 0.0015 + 0.0015 = 0.003
        assert abs(result.estimated_usd - 0.003) < 1e-9
