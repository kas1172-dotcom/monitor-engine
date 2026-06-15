from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from dateutil import parser as dateutil_parser

from monitor_engine.collectors.base import CollectResult, SourceHandler, stable_id, _DEFAULT_TIMEOUT
from monitor_engine.models import HtmlListSource, RawItem

# Fast-path formats tried before handing off to dateutil
_DATE_FORMATS = (
    "%B %d, %Y",   # June 10, 2026
    "%b %d, %Y",   # Jun 10, 2026
    "%Y-%m-%d",    # 2026-06-10
    "%m/%d/%Y",    # 06/10/2026
    "%d %B %Y",    # 10 June 2026
    "%d %b %Y",    # 10 Jun 2026
)


def _try_parse_date(text: str) -> tuple[datetime | None, bool]:
    """
    Returns (dt, parse_failed).
    parse_failed=True when text was non-empty but no parser could interpret it.
    """
    text = text.strip()
    if not text:
        return None, False
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc), False
        except ValueError:
            continue
    try:
        dt = dateutil_parser.parse(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt, False
    except (ValueError, OverflowError, TypeError):
        return None, True


def _resolve_url(href: str, base_url: str) -> str:
    """Turn relative hrefs into absolute URLs using the source's base URL."""
    if href.startswith(("http://", "https://")):
        return href
    parsed = urlparse(base_url)
    if href.startswith("//"):
        return f"{parsed.scheme}:{href}"
    if href.startswith("/"):
        return f"{parsed.scheme}://{parsed.netloc}{href}"
    base_dir = parsed.path.rsplit("/", 1)[0]
    return f"{parsed.scheme}://{parsed.netloc}{base_dir}/{href}"


class HtmlListHandler(SourceHandler):
    def collect(
        self,
        source: HtmlListSource,
        *,
        days_back: int,
        max_items: int,
    ) -> CollectResult:
        effective_days_back = source.days_back if source.days_back is not None else days_back
        effective_timeout = source.timeout if source.timeout is not None else _DEFAULT_TIMEOUT

        base_url = str(source.url)
        resp = self.session.get(base_url, timeout=effective_timeout)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        cutoff = datetime.now(timezone.utc) - timedelta(days=effective_days_back)
        now = datetime.now(timezone.utc)
        items: list[RawItem] = []
        date_parse_failures = 0

        for el in soup.select(source.item_selector):
            if len(items) >= max_items:
                break

            title_el = el.select_one(source.title_selector)
            link_el = el.select_one(source.link_selector)
            if not title_el or not link_el:
                continue

            title = title_el.get_text(strip=True)
            href = link_el.get("href", "")
            if not href:
                continue
            url = _resolve_url(href, base_url)

            pub: datetime | None = None
            if source.date_selector:
                date_el = el.select_one(source.date_selector)
                if date_el:
                    pub, failed = _try_parse_date(date_el.get_text(strip=True))
                    if failed:
                        date_parse_failures += 1

            if pub is not None and pub < cutoff:
                continue

            items.append(
                RawItem(
                    id=stable_id(source.id, url),
                    title=title,
                    summary=None,
                    url=url,
                    published_date=pub,
                    date_unknown=pub is None,
                    discovery_date=now,
                    source_name=source.name,
                    source_type="html_list",
                )
            )

        return CollectResult(items=items, date_parse_failures=date_parse_failures)
