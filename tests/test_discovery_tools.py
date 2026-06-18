"""Tests for the source-discovery agent's deterministic oracle tools."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from tooling.discovery_tools import fetch_sample, probe_source

FIXTURES = Path(__file__).parent / "fixtures"


def _mock_session(*, text: str = "", content: bytes | None = None,
                  status: int = 200, content_type: str = "") -> MagicMock:
    resp = MagicMock()
    resp.text = text
    resp.content = content if content is not None else text.encode()
    resp.status_code = status
    resp.url = "https://example.com/x"
    resp.headers = {"content-type": content_type}
    try:
        resp.json.return_value = json.loads(text)
    except (ValueError, TypeError):
        resp.json.side_effect = ValueError("not json")
    resp.raise_for_status.return_value = None
    session = MagicMock()
    session.get.return_value = resp
    return session


# ─── fetch_sample ───────────────────────────────────────────────────────────

class TestFetchSample:
    def test_returns_truncated_body(self):
        s = _mock_session(text="x" * 9000, content_type="text/html")
        out = fetch_sample("https://example.com", session=s, max_chars=4000)
        assert out["ok"] is True
        assert len(out["body_sample"]) == 4000
        assert out["truncated"] is True
        assert out["content_type"] == "text/html"

    def test_http_error_status_is_not_ok(self):
        s = _mock_session(text="nope", status=404)
        out = fetch_sample("https://example.com", session=s)
        assert out["ok"] is False
        assert out["status"] == 404

    def test_network_error_is_caught(self):
        s = MagicMock()
        s.get.side_effect = Exception("connection refused")
        out = fetch_sample("https://example.com", session=s)
        assert out["ok"] is False
        assert "connection refused" in out["error"]

    def test_passes_user_agent_override(self):
        s = _mock_session(text="ok")
        fetch_sample("https://example.com", session=s, user_agent="UA/1.0")
        assert s.get.call_args.kwargs["headers"] == {"User-Agent": "UA/1.0"}


# ─── probe_source (the oracle) ──────────────────────────────────────────────

class TestProbeSource:
    def test_rss_source_ok(self):
        content = (FIXTURES / "sample_rss.xml").read_bytes()
        out = probe_source(
            {"type": "rss", "id": "f", "name": "Feed", "url": "https://example.com/feed"},
            session=_mock_session(content=content),
        )
        assert out["ok"] is True
        assert out["items_collected"] > 0
        assert out["sample"]["title"]

    def test_json_api_source_ok(self):
        text = (FIXTURES / "sample_api.json").read_text()
        out = probe_source(
            {
                "type": "json_api", "id": "a", "name": "API",
                "url": "https://api.example.com/v2/search",
                "item_path": "$.opportunitiesData",
                "field_map": {"title": "title", "url": "uiLink", "published_at": "postedDate"},
            },
            session=_mock_session(text=text),
        )
        assert out["ok"] is True
        assert out["items_collected"] > 0

    def test_html_list_source_ok(self):
        text = (FIXTURES / "sample_html.html").read_text()
        out = probe_source(
            {
                "type": "html_list", "id": "h", "name": "HTML",
                "url": "https://example.com/news",
                "item_selector": "ul.news-list li", "title_selector": "a",
                "link_selector": "a[href]", "date_selector": "span.date",
            },
            session=_mock_session(text=text),
        )
        assert out["ok"] is True
        assert out["items_collected"] > 0

    def test_invalid_config_reports_error_not_raise(self):
        out = probe_source({"type": "rss", "id": "x"})  # missing required fields
        assert out["ok"] is False
        assert "invalid source config" in out["error"]

    def test_unknown_type_reports_error(self):
        out = probe_source({"type": "carrier_pigeon", "id": "x", "name": "n", "url": "https://e.com"})
        assert out["ok"] is False

    def test_zero_items_is_not_ok(self):
        out = probe_source(
            {"type": "json_api", "id": "a", "name": "API", "url": "https://api.example.com/x",
             "item_path": "$.results", "field_map": {"title": "title", "url": "url"}},
            session=_mock_session(text=json.dumps({"results": []})),
        )
        assert out["ok"] is False
        assert out["items_collected"] == 0

    def test_collector_exception_is_caught(self):
        s = MagicMock()
        s.get.side_effect = Exception("boom")
        out = probe_source(
            {"type": "rss", "id": "f", "name": "Feed", "url": "https://example.com/feed"},
            session=s,
        )
        assert out["ok"] is False
        assert "boom" in out["error"]
