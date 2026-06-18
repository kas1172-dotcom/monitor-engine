"""
Unit tests for the collection layer.
All tests use recorded fixture responses; no live network calls are made.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from monitor_engine.collectors.base import (
    CollectResult,
    check_env_vars,
    make_session,
    per_source_headers,
    stable_id,
    _DEFAULT_USER_AGENT,
)
from monitor_engine.collectors.html_list import HtmlListHandler
from monitor_engine.collectors.json_api import JsonApiHandler
from monitor_engine.collectors.rss import RssHandler
from monitor_engine.models import (
    Branding,
    Cadence,
    ClientConfig,
    CostCaps,
    Edition,
    HtmlListSource,
    JsonApiSource,
    KeywordPrefilter,
    RssSource,
    ScoringRubric,
    TierThresholds,
)

FIXTURES = Path(__file__).parent / "fixtures"


# ── helpers ───────────────────────────────────────────────────────────────────


def _mock_session(*, text: str = "", content: bytes | None = None) -> MagicMock:
    """
    Build a MagicMock requests.Session whose .get() returns a canned response.
    Pass `content` for binary payloads (RSS XML); pass `text` for string payloads (JSON, HTML).
    """
    resp = MagicMock()
    resp.text = text
    resp.content = content if content is not None else text.encode()
    try:
        resp.json.return_value = json.loads(text)
    except (ValueError, TypeError):
        resp.json.side_effect = ValueError("response is not JSON")
    resp.raise_for_status.return_value = None

    session = MagicMock()
    session.get.return_value = resp
    session.post.return_value = resp
    return session


def _rss_source(**kwargs) -> RssSource:
    defaults = dict(type="rss", id="defense_news", name="Defense News", url="https://example.com/feed")
    return RssSource(**(defaults | kwargs))


def _api_source(**kwargs) -> JsonApiSource:
    defaults = dict(
        type="json_api",
        id="sam_opportunities",
        name="SAM.gov",
        url="https://api.sam.example.gov/v2/search",
        item_path="$.opportunitiesData",
        field_map={
            "title": "title",
            "url": "uiLink",
            "published_at": "postedDate",
            "body": "description",
        },
    )
    return JsonApiSource(**(defaults | kwargs))


def _html_source(**kwargs) -> HtmlListSource:
    defaults = dict(
        type="html_list",
        id="af_solicitations",
        name="AF Solicitations",
        url="https://www.af.example.mil/solicitations/",
        item_selector="ul.news-list li",
        title_selector="a",
        link_selector="a[href]",
        date_selector="span.date",
    )
    return HtmlListSource(**(defaults | kwargs))


def _minimal_config(sources=None) -> ClientConfig:
    return ClientConfig(
        branding=Branding(name="Test", accent_color="#123456"),
        editions=[
            Edition(
                id="ed1",
                label="Ed 1",
                audience_description="x",
                analysis_instructions="y",
                categories=[],
            )
        ],
        scoring_rubric=ScoringRubric(thresholds=TierThresholds(), never_discard=[]),
        sources=sources or [],
        keyword_prefilter=KeywordPrefilter(include=["test"]),
        cadence=Cadence(cron="0 6 * * 1-5"),
        cost_caps=CostCaps(),
    )


# ── RssHandler ────────────────────────────────────────────────────────────────


class TestRssHandler:
    def test_returns_recent_items(self):
        content = (FIXTURES / "sample_rss.xml").read_bytes()
        result = RssHandler(_mock_session(content=content)).collect(_rss_source(), days_back=30, max_items=10)
        # Fixture: 2 recent, 1 no-date (passes through), 1 old (filtered)
        assert len(result.items) == 3

    def test_old_item_filtered_by_days_back(self):
        content = (FIXTURES / "sample_rss.xml").read_bytes()
        result = RssHandler(_mock_session(content=content)).collect(_rss_source(), days_back=30, max_items=10)
        urls = [i.url for i in result.items]
        assert "https://defensenews.example.com/article/old" not in urls

    def test_respects_max_items(self):
        content = (FIXTURES / "sample_rss.xml").read_bytes()
        result = RssHandler(_mock_session(content=content)).collect(_rss_source(), days_back=30, max_items=1)
        assert len(result.items) == 1

    def test_item_has_correct_fields(self):
        content = (FIXTURES / "sample_rss.xml").read_bytes()
        result = RssHandler(_mock_session(content=content)).collect(_rss_source(), days_back=30, max_items=10)
        item = result.items[0]
        assert item.title == "Pentagon awards $2.3B contract for next-gen fighter"
        assert item.url == "https://defensenews.example.com/article/1"
        assert item.source_type == "rss"
        assert item.source_name == "Defense News"
        assert item.id == stable_id("defense_news", item.url)
        assert isinstance(item.discovery_date, datetime)

    def test_dated_item_sets_date_unknown_false(self):
        content = (FIXTURES / "sample_rss.xml").read_bytes()
        result = RssHandler(_mock_session(content=content)).collect(_rss_source(), days_back=30, max_items=10)
        dated = next(i for i in result.items if i.url == "https://defensenews.example.com/article/1")
        assert dated.published_date is not None
        assert dated.date_unknown is False

    def test_item_without_date_sets_date_unknown_true(self):
        content = (FIXTURES / "sample_rss.xml").read_bytes()
        result = RssHandler(_mock_session(content=content)).collect(_rss_source(), days_back=30, max_items=10)
        no_date = next(i for i in result.items if i.url == "https://defensenews.example.com/article/3")
        assert no_date.published_date is None
        assert no_date.date_unknown is True

    def test_html_tags_stripped_from_summary(self):
        content = (FIXTURES / "sample_rss.xml").read_bytes()
        result = RssHandler(_mock_session(content=content)).collect(_rss_source(), days_back=30, max_items=10)
        assert result.items[0].summary is not None
        assert "<p>" not in result.items[0].summary
        assert "Pentagon" in result.items[0].summary

    def test_item_id_is_stable_hash(self):
        content = (FIXTURES / "sample_rss.xml").read_bytes()
        result = RssHandler(_mock_session(content=content)).collect(_rss_source(), days_back=30, max_items=10)
        for item in result.items:
            assert item.id == stable_id("defense_news", item.url)
            assert len(item.id) == 16

    def test_returns_collect_result(self):
        content = (FIXTURES / "sample_rss.xml").read_bytes()
        result = RssHandler(_mock_session(content=content)).collect(_rss_source(), days_back=30, max_items=10)
        assert isinstance(result, CollectResult)
        assert isinstance(result.date_parse_failures, int)

    def test_per_source_days_back_overrides_global(self):
        content = (FIXTURES / "sample_rss.xml").read_bytes()
        # Global window of 1 day would filter out the June 10 and 2020 articles.
        # Source override of 36500 days (100 years) passes all 4 fixture entries,
        # including the 2020 article that the global window would otherwise drop.
        source = _rss_source(days_back=36500)
        result = RssHandler(_mock_session(content=content)).collect(source, days_back=1, max_items=10)
        assert len(result.items) == 4  # all entries pass the 100-year window

    def test_per_source_timeout_passed_to_session(self):
        content = (FIXTURES / "sample_rss.xml").read_bytes()
        session = _mock_session(content=content)
        source = _rss_source(timeout=42)
        RssHandler(session).collect(source, days_back=30, max_items=10)
        assert session.get.call_args.kwargs["timeout"] == 42


# ── JsonApiHandler ────────────────────────────────────────────────────────────


class TestJsonApiHandler:
    def test_returns_items(self):
        text = (FIXTURES / "sample_api.json").read_text()
        result = JsonApiHandler(_mock_session(text=text)).collect(_api_source(), days_back=30, max_items=10)
        assert len(result.items) == 3

    def test_field_map_title_and_url(self):
        text = (FIXTURES / "sample_api.json").read_text()
        result = JsonApiHandler(_mock_session(text=text)).collect(_api_source(), days_back=30, max_items=10)
        assert result.items[0].title == "IDIQ for Cybersecurity Services"
        assert result.items[0].url == "https://sam.example.gov/opp/abc123"

    def test_field_map_summary(self):
        text = (FIXTURES / "sample_api.json").read_text()
        result = JsonApiHandler(_mock_session(text=text)).collect(_api_source(), days_back=30, max_items=10)
        assert result.items[0].summary is not None
        assert "cybersecurity" in result.items[0].summary.lower()

    def test_date_parsed(self):
        text = (FIXTURES / "sample_api.json").read_text()
        result = JsonApiHandler(_mock_session(text=text)).collect(_api_source(), days_back=30, max_items=10)
        assert result.items[0].published_date == datetime(2026, 6, 10, tzinfo=timezone.utc)
        assert result.items[0].date_unknown is False

    def test_respects_max_items(self):
        text = (FIXTURES / "sample_api.json").read_text()
        result = JsonApiHandler(_mock_session(text=text)).collect(_api_source(), days_back=30, max_items=2)
        assert len(result.items) == 2

    def test_api_key_sent_in_header(self, monkeypatch):
        monkeypatch.setenv("MY_API_KEY", "secret-token")
        source = _api_source(auth_header="X-Api-Key", auth_env_var="MY_API_KEY")
        session = _mock_session(text='{"opportunitiesData": []}')
        JsonApiHandler(session).collect(source, days_back=7, max_items=10)
        assert session.get.call_args.kwargs["headers"]["X-Api-Key"] == "secret-token"

    def test_no_auth_header_when_no_env_var(self):
        source = _api_source()
        session = _mock_session(text='{"opportunitiesData": []}')
        JsonApiHandler(session).collect(source, days_back=7, max_items=10)
        assert session.get.call_args.kwargs.get("headers") == {}

    def test_item_path_nested(self):
        payload = json.dumps({"results": {"items": [
            {"title": "Nested item", "link": "https://example.com/1", "date": "2026-06-10"}
        ]}})
        source = _api_source(
            item_path="$.results.items",
            field_map={"title": "title", "url": "link", "published_at": "date"},
        )
        result = JsonApiHandler(_mock_session(text=payload)).collect(source, days_back=30, max_items=10)
        assert len(result.items) == 1
        assert result.items[0].title == "Nested item"

    def test_missing_url_field_skipped(self):
        payload = json.dumps({"opportunitiesData": [
            {"title": "No URL item"},
            {"title": "Has URL", "uiLink": "https://example.com/2", "postedDate": "2026-06-10"},
        ]})
        result = JsonApiHandler(_mock_session(text=payload)).collect(_api_source(), days_back=30, max_items=10)
        assert len(result.items) == 1
        assert result.items[0].title == "Has URL"

    def test_post_sends_request_body_as_json(self):
        text = (FIXTURES / "sample_api.json").read_text()
        session = _mock_session(text=text)
        source = _api_source(method="POST", request_body={"q": "defense", "limit": 50})
        result = JsonApiHandler(session).collect(source, days_back=36500, max_items=10)
        # POST path taken, not GET.
        assert session.post.called
        assert not session.get.called
        assert session.post.call_args.kwargs["json"] == {"q": "defense", "limit": 50}
        # And it still parses items exactly like the GET path.
        assert len(result.items) == 3

    def test_post_with_no_body_sends_empty_object(self):
        session = _mock_session(text='{"opportunitiesData": []}')
        source = _api_source(method="POST")  # request_body defaults to None
        JsonApiHandler(session).collect(source, days_back=7, max_items=10)
        assert session.post.call_args.kwargs["json"] == {}

    def test_get_remains_default(self):
        text = (FIXTURES / "sample_api.json").read_text()
        session = _mock_session(text=text)
        JsonApiHandler(session).collect(_api_source(), days_back=36500, max_items=10)
        assert session.get.called
        assert not session.post.called

    def test_unparseable_date_counts_as_failure(self):
        payload = json.dumps({"opportunitiesData": [
            {"title": "Bad date", "uiLink": "https://example.com/1", "postedDate": "not-a-date"},
            {"title": "Good date", "uiLink": "https://example.com/2", "postedDate": "2026-06-10"},
        ]})
        result = JsonApiHandler(_mock_session(text=payload)).collect(_api_source(), days_back=30, max_items=10)
        assert result.date_parse_failures == 1
        bad = next(i for i in result.items if i.title == "Bad date")
        assert bad.date_unknown is True

    def test_dateutil_fallback_parses_verbose_date(self):
        payload = json.dumps({"opportunitiesData": [
            {"title": "Verbose date", "uiLink": "https://example.com/1",
             "postedDate": "Wednesday, June 10, 2026"},
        ]})
        result = JsonApiHandler(_mock_session(text=payload)).collect(_api_source(), days_back=30, max_items=10)
        assert result.date_parse_failures == 0
        assert result.items[0].published_date is not None
        assert result.items[0].published_date.year == 2026

    def test_per_source_timeout_passed_to_session(self):
        text = (FIXTURES / "sample_api.json").read_text()
        session = _mock_session(text=text)
        source = _api_source(timeout=30)
        JsonApiHandler(session).collect(source, days_back=30, max_items=10)
        assert session.get.call_args.kwargs["timeout"] == 30

    def test_per_source_days_back_overrides_global(self):
        text = (FIXTURES / "sample_api.json").read_text()
        # All fixture items are dated 2026-06-08 through 2026-06-10.
        # With a 1-day global and a 36500-day source override, all should pass.
        source = _api_source(days_back=36500)
        result = JsonApiHandler(_mock_session(text=text)).collect(source, days_back=1, max_items=10)
        assert len(result.items) == 3


# ── HtmlListHandler ───────────────────────────────────────────────────────────


class TestHtmlListHandler:
    def test_returns_items(self):
        text = (FIXTURES / "sample_html.html").read_text()
        result = HtmlListHandler(_mock_session(text=text)).collect(_html_source(), days_back=30, max_items=10)
        # Fixture: 2 items with dates, 1 no-date (passes through), 1 malformed (no anchor, skipped)
        assert len(result.items) == 3

    def test_malformed_entry_skipped(self):
        text = (FIXTURES / "sample_html.html").read_text()
        result = HtmlListHandler(_mock_session(text=text)).collect(_html_source(), days_back=30, max_items=10)
        assert "Broken entry with no anchor" not in [i.title for i in result.items]

    def test_titles_extracted(self):
        text = (FIXTURES / "sample_html.html").read_text()
        result = HtmlListHandler(_mock_session(text=text)).collect(_html_source(), days_back=30, max_items=10)
        assert result.items[0].title == "C-130 Depot Maintenance Solicitation"
        assert result.items[1].title == "F-35 Parts Supply Request for Quotation"

    def test_relative_url_resolved(self):
        text = (FIXTURES / "sample_html.html").read_text()
        result = HtmlListHandler(_mock_session(text=text)).collect(_html_source(), days_back=30, max_items=10)
        assert result.items[0].url == "https://www.af.example.mil/solicitations/sol-001"
        assert result.items[1].url == "https://www.af.example.mil/solicitations/sol-002"

    def test_date_parsed(self):
        text = (FIXTURES / "sample_html.html").read_text()
        result = HtmlListHandler(_mock_session(text=text)).collect(_html_source(), days_back=30, max_items=10)
        assert result.items[0].published_date == datetime(2026, 6, 10, tzinfo=timezone.utc)
        assert result.items[0].date_unknown is False

    def test_item_without_date_selector_sets_date_unknown(self):
        text = (FIXTURES / "sample_html.html").read_text()
        result = HtmlListHandler(_mock_session(text=text)).collect(
            _html_source(date_selector=None), days_back=30, max_items=10
        )
        assert all(i.date_unknown is True for i in result.items)

    def test_item_with_missing_date_element_sets_date_unknown(self):
        text = (FIXTURES / "sample_html.html").read_text()
        result = HtmlListHandler(_mock_session(text=text)).collect(_html_source(), days_back=30, max_items=10)
        no_date = next(i for i in result.items if "no date" in i.title.lower())
        assert no_date.date_unknown is True

    def test_respects_max_items(self):
        text = (FIXTURES / "sample_html.html").read_text()
        result = HtmlListHandler(_mock_session(text=text)).collect(_html_source(), days_back=30, max_items=1)
        assert len(result.items) == 1

    def test_source_type_and_name(self):
        text = (FIXTURES / "sample_html.html").read_text()
        result = HtmlListHandler(_mock_session(text=text)).collect(_html_source(), days_back=30, max_items=10)
        assert all(i.source_type == "html_list" for i in result.items)
        assert all(i.source_name == "AF Solicitations" for i in result.items)

    def test_unparseable_date_counts_as_failure(self):
        html = """<html><body><ul class="news-list">
          <li><a href="/sol-1">Item with garbled date</a><span class="date">??/??/????</span></li>
          <li><a href="/sol-2">Item with good date</a><span class="date">June 10, 2026</span></li>
        </ul></body></html>"""
        result = HtmlListHandler(_mock_session(text=html)).collect(_html_source(), days_back=30, max_items=10)
        assert result.date_parse_failures == 1

    def test_dateutil_fallback_parses_verbose_date(self):
        html = """<html><body><ul class="news-list">
          <li><a href="/sol-1">Item</a><span class="date">Wednesday, June 10, 2026</span></li>
        </ul></body></html>"""
        result = HtmlListHandler(_mock_session(text=html)).collect(_html_source(), days_back=30, max_items=10)
        assert result.date_parse_failures == 0
        assert result.items[0].published_date is not None

    def test_per_source_timeout_passed_to_session(self):
        text = (FIXTURES / "sample_html.html").read_text()
        session = _mock_session(text=text)
        HtmlListHandler(session).collect(_html_source(timeout=20), days_back=30, max_items=10)
        assert session.get.call_args.kwargs["timeout"] == 20

    def test_per_source_days_back_overrides_global(self):
        text = (FIXTURES / "sample_html.html").read_text()
        source = _html_source(days_back=36500)
        result = HtmlListHandler(_mock_session(text=text)).collect(source, days_back=1, max_items=10)
        assert len(result.items) == 3


# ── check_env_vars ────────────────────────────────────────────────────────────


class TestCheckEnvVars:
    def test_passes_when_var_present(self, monkeypatch):
        monkeypatch.setenv("MY_KEY", "value")
        config = _minimal_config(sources=[_api_source(auth_env_var="MY_KEY", auth_header="X-Key")])
        check_env_vars(config)

    def test_raises_on_missing_var(self, monkeypatch):
        monkeypatch.delenv("MISSING_KEY", raising=False)
        config = _minimal_config(sources=[_api_source(auth_env_var="MISSING_KEY", auth_header="X-Key")])
        with pytest.raises(EnvironmentError, match="MISSING_KEY"):
            check_env_vars(config)

    def test_lists_all_missing_vars_in_one_error(self, monkeypatch):
        monkeypatch.delenv("KEY_A", raising=False)
        monkeypatch.delenv("KEY_B", raising=False)
        sources = [
            _api_source(id="s1", auth_env_var="KEY_A", auth_header="X-A"),
            _api_source(id="s2", auth_env_var="KEY_B", auth_header="X-B"),
        ]
        with pytest.raises(EnvironmentError) as exc_info:
            check_env_vars(_minimal_config(sources=sources))
        assert "KEY_A" in str(exc_info.value)
        assert "KEY_B" in str(exc_info.value)

    def test_rss_sources_have_no_env_var_requirement(self):
        check_env_vars(_minimal_config(sources=[_rss_source()]))

    def test_json_api_without_auth_has_no_env_var_requirement(self):
        check_env_vars(_minimal_config(sources=[_api_source()]))


# ── stable_id ─────────────────────────────────────────────────────────────────


class TestStableId:
    def test_same_inputs_give_same_id(self):
        assert stable_id("src", "https://example.com/a") == stable_id("src", "https://example.com/a")

    def test_different_urls_give_different_ids(self):
        assert stable_id("src", "https://example.com/a") != stable_id("src", "https://example.com/b")

    def test_different_source_ids_give_different_ids(self):
        assert stable_id("src_a", "https://example.com") != stable_id("src_b", "https://example.com")

    def test_id_is_16_hex_chars(self):
        result = stable_id("src", "https://example.com")
        assert len(result) == 16
        assert all(c in "0123456789abcdef" for c in result)


# ── SourceHealth (via models) ─────────────────────────────────────────────────


class TestSourceHealthModel:
    def test_defaults(self):
        from monitor_engine.models import SourceHealth
        h = SourceHealth(source_id="s1", items_collected=5, zero_results=False)
        assert h.error is None
        assert h.date_parse_failures == 0

    def test_error_field(self):
        from monitor_engine.models import SourceHealth
        h = SourceHealth(source_id="s1", items_collected=0, zero_results=True, error="timeout")
        assert h.error == "timeout"
        assert h.zero_results is True


# ── User-Agent (default browser UA + per-source override) ─────────────────────


class TestUserAgent:
    def test_session_default_is_browser_like(self):
        session = make_session()
        ua = session.headers["User-Agent"]
        assert "Mozilla/5.0" in ua and "Chrome/" in ua
        assert "monitor-engine" not in ua   # the old bot UA that triggered 403s

    def test_per_source_headers_empty_without_override(self):
        assert per_source_headers(_rss_source()) == {}

    def test_per_source_headers_uses_override(self):
        src = _rss_source(user_agent="CustomAgent/9.9")
        assert per_source_headers(src) == {"User-Agent": "CustomAgent/9.9"}

    def test_rss_passes_override_header_to_get(self):
        content = (FIXTURES / "sample_rss.xml").read_bytes()
        session = _mock_session(content=content)
        RssHandler(session).collect(
            _rss_source(user_agent="UA-RSS/1.0"), days_back=30, max_items=5
        )
        assert session.get.call_args.kwargs["headers"] == {"User-Agent": "UA-RSS/1.0"}

    def test_html_passes_override_header_to_get(self):
        session = _mock_session(text="<html></html>")
        HtmlListHandler(session).collect(
            _html_source(user_agent="UA-HTML/1.0"), days_back=30, max_items=5
        )
        assert session.get.call_args.kwargs["headers"] == {"User-Agent": "UA-HTML/1.0"}

    def test_json_api_override_merges_with_no_auth(self):
        session = _mock_session(text=json.dumps({"opportunitiesData": []}))
        JsonApiHandler(session).collect(
            _api_source(user_agent="UA-API/1.0"), days_back=30, max_items=5
        )
        assert session.get.call_args.kwargs["headers"]["User-Agent"] == "UA-API/1.0"


# ── json_api url_template and base_url ────────────────────────────────────────


class TestJsonApiUrlBuilding:
    def _payload(self, results):
        return json.dumps({"results": results})

    def test_url_template_substitutes_record_field(self):
        src = _api_source(
            item_path="$.results",
            url_template="https://host/page?ID={k_number}",
            field_map={"title": "device_name", "published_at": "decision_date"},
        )
        session = _mock_session(text=self._payload([
            {"k_number": "K261154", "device_name": "Widget", "decision_date": "2026-06-01"},
        ]))
        result = JsonApiHandler(session).collect(src, days_back=3650, max_items=5)
        assert len(result.items) == 1
        assert result.items[0].url == "https://host/page?ID=K261154"

    def test_url_template_skips_item_missing_field(self):
        src = _api_source(
            item_path="$.results",
            url_template="https://host/page?ID={k_number}",
            field_map={"title": "device_name"},
        )
        session = _mock_session(text=self._payload([
            {"device_name": "No K number here", "decision_date": "2026-06-01"},  # k_number absent
        ]))
        result = JsonApiHandler(session).collect(src, days_back=3650, max_items=5)
        assert result.items == []

    def test_base_url_resolves_relative_url(self):
        src = _api_source(
            item_path="$.results",
            base_url="https://www.courtlistener.com",
            field_map={"title": "caseName", "url": "absolute_url", "published_at": "dateFiled"},
        )
        session = _mock_session(text=self._payload([
            {"caseName": "A v B", "absolute_url": "/opinion/123/a-v-b/", "dateFiled": "2026-06-01"},
        ]))
        result = JsonApiHandler(session).collect(src, days_back=3650, max_items=5)
        assert result.items[0].url == "https://www.courtlistener.com/opinion/123/a-v-b/"

    def test_absolute_url_left_unchanged_with_base_url(self):
        src = _api_source(
            item_path="$.results",
            base_url="https://www.courtlistener.com",
            field_map={"title": "caseName", "url": "absolute_url", "published_at": "dateFiled"},
        )
        session = _mock_session(text=self._payload([
            {"caseName": "A v B", "absolute_url": "https://other.example/x", "dateFiled": "2026-06-01"},
        ]))
        result = JsonApiHandler(session).collect(src, days_back=3650, max_items=5)
        assert result.items[0].url == "https://other.example/x"

    def test_url_template_map_translates_field_value(self):
        # Congress case: API "type" ("HR"/"S") → human slug ("house-bill"/"senate-bill").
        src = _api_source(
            item_path="$.results",
            url_template="https://www.congress.gov/bill/119th-congress/{type}/{number}",
            url_template_map={"type": {"HR": "house-bill", "S": "senate-bill"}},
            field_map={"title": "title", "published_at": "updateDate"},
        )
        session = _mock_session(text=self._payload([
            {"type": "HR", "number": "7086", "title": "A bill", "updateDate": "2026-06-15"},
            {"type": "S", "number": "4114", "title": "Another", "updateDate": "2026-06-14"},
        ]))
        result = JsonApiHandler(session).collect(src, days_back=3650, max_items=5)
        assert result.items[0].url == "https://www.congress.gov/bill/119th-congress/house-bill/7086"
        assert result.items[1].url == "https://www.congress.gov/bill/119th-congress/senate-bill/4114"

    def test_url_template_map_skips_unmapped_value(self):
        # A bill type with no mapping must be skipped, not linked to a wrong URL.
        src = _api_source(
            item_path="$.results",
            url_template="https://www.congress.gov/bill/119th-congress/{type}/{number}",
            url_template_map={"type": {"HR": "house-bill"}},
            field_map={"title": "title"},
        )
        session = _mock_session(text=self._payload([
            {"type": "HJRES", "number": "9", "title": "Unmapped type"},
        ]))
        result = JsonApiHandler(session).collect(src, days_back=3650, max_items=5)
        assert result.items == []

    def test_url_template_takes_precedence_over_field_map(self):
        src = _api_source(
            item_path="$.results",
            url_template="https://host/{k_number}",
            field_map={"title": "device_name", "url": "ignored_field"},
        )
        session = _mock_session(text=self._payload([
            {"k_number": "K1", "device_name": "W", "ignored_field": "https://wrong/"},
        ]))
        result = JsonApiHandler(session).collect(src, days_back=3650, max_items=5)
        assert result.items[0].url == "https://host/K1"
