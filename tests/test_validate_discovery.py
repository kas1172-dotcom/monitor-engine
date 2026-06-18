"""Tests for the discovery-validation coverage comparison (the pure, no-network part)."""
from __future__ import annotations

from tooling.validate_discovery import _domain, summarize_coverage


class TestDomain:
    def test_strips_www_and_lowercases(self):
        assert _domain("https://WWW.Example.com/feed") == "example.com"

    def test_no_www(self):
        assert _domain("https://api.congress.gov/v3/bill") == "api.congress.gov"

    def test_empty_url(self):
        assert _domain("") == ""


class TestSummarizeCoverage:
    def test_full_overlap_is_100pct(self):
        disc = [{"type": "rss", "url": "https://example.com/a"}]
        ref = [{"type": "rss", "url": "https://www.example.com/b"}]  # same domain, www stripped
        s = summarize_coverage(disc, ref)
        assert s["coverage_pct"] == 100.0
        assert s["shared_domains"] == ["example.com"]
        assert s["missed_domains"] == []

    def test_partial_overlap(self):
        disc = [{"type": "rss", "url": "https://a.com/x"}]
        ref = [
            {"type": "rss", "url": "https://a.com/y"},
            {"type": "json_api", "url": "https://b.com/api"},
        ]
        s = summarize_coverage(disc, ref)
        assert s["coverage_pct"] == 50.0
        assert s["missed_domains"] == ["b.com"]

    def test_type_breakdown(self):
        disc = [
            {"type": "rss", "url": "https://a.com/x"},
            {"type": "rss", "url": "https://b.com/x"},
            {"type": "json_api", "url": "https://c.com/x"},
        ]
        s = summarize_coverage(disc, reference=[])
        assert s["discovered_types"] == {"rss": 2, "json_api": 1}
        assert s["coverage_pct"] is None  # no reference to compare against

    def test_counts(self):
        s = summarize_coverage(
            discovered=[{"type": "rss", "url": "https://a.com"}],
            reference=[{"type": "rss", "url": "https://a.com"}, {"type": "rss", "url": "https://b.com"}],
        )
        assert s["discovered_count"] == 1
        assert s["reference_count"] == 2
