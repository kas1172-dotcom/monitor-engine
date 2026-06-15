from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import feedparser

from monitor_engine.collectors.base import CollectResult, SourceHandler, stable_id, _DEFAULT_TIMEOUT
from monitor_engine.models import RawItem, RssSource

_TAG_RE = re.compile(r"<[^>]+>")


def _parse_date(entry: feedparser.FeedParserDict) -> tuple[datetime | None, bool]:
    """
    Returns (dt, parse_failed).
    parse_failed=True when feedparser gave us struct_time data that failed datetime conversion.
    """
    had_value = False
    for attr in ("published_parsed", "updated_parsed", "created_parsed"):
        t = getattr(entry, attr, None)
        if t is not None:
            had_value = True
            try:
                return datetime(t[0], t[1], t[2], t[3], t[4], t[5], tzinfo=timezone.utc), False
            except (ValueError, TypeError):
                continue
    return None, had_value  # failed only when feedparser had data we couldn't convert


def _strip_tags(text: str) -> str:
    return _TAG_RE.sub(" ", text).strip()


class RssHandler(SourceHandler):
    def collect(
        self,
        source: RssSource,
        *,
        days_back: int,
        max_items: int,
    ) -> CollectResult:
        effective_days_back = source.days_back if source.days_back is not None else days_back
        effective_timeout = source.timeout if source.timeout is not None else _DEFAULT_TIMEOUT

        resp = self.session.get(str(source.url), timeout=effective_timeout)
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)

        cutoff = datetime.now(timezone.utc) - timedelta(days=effective_days_back)
        now = datetime.now(timezone.utc)
        items: list[RawItem] = []
        date_parse_failures = 0

        for entry in feed.entries:
            if len(items) >= max_items:
                break
            url: str = entry.get("link", "")
            if not url:
                continue

            pub, failed = _parse_date(entry)
            if failed:
                date_parse_failures += 1
            if pub is not None and pub < cutoff:
                continue

            raw_summary = entry.get("summary") or entry.get("description") or ""
            summary = _strip_tags(raw_summary) or None

            items.append(
                RawItem(
                    id=stable_id(source.id, url),
                    title=entry.get("title", "(no title)").strip(),
                    summary=summary,
                    url=url,
                    published_date=pub,
                    date_unknown=pub is None,
                    discovery_date=now,
                    source_name=source.name,
                    source_type="rss",
                )
            )

        return CollectResult(items=items, date_parse_failures=date_parse_failures)
