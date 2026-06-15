from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

from dateutil import parser as dateutil_parser

from monitor_engine.collectors.base import CollectResult, SourceHandler, stable_id, _DEFAULT_TIMEOUT
from monitor_engine.models import JsonApiSource, RawItem

# Fast-path formats tried before handing off to dateutil
_DATE_FORMATS = (
    "%Y-%m-%dT%H:%M:%S.%fZ",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d",
    "%m/%d/%Y",
)


def _resolve_path(data: Any, path: str) -> Any:
    """
    Traverse a dot-notation path such as '$.opportunitiesData' or 'results.items'.
    Strips leading '$.' prefix.  Integer segments index into lists.
    """
    path = path.lstrip("$.")
    if not path:
        return data
    for key in path.split("."):
        if isinstance(data, dict):
            data = data[key]
        elif isinstance(data, list):
            data = data[int(key)]
        else:
            raise KeyError(f"Cannot traverse {type(data).__name__} with key {key!r}")
    return data


def _parse_date(value: str | None) -> tuple[datetime | None, bool]:
    """
    Returns (dt, parse_failed).
    parse_failed=True when value was non-empty but no parser could interpret it.
    Rule: never guess or fabricate — return (None, True) on any ambiguity.
    """
    if not value:
        return None, False
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc), False
        except ValueError:
            continue
    try:
        dt = dateutil_parser.parse(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt, False
    except (ValueError, OverflowError, TypeError):
        return None, True


class JsonApiHandler(SourceHandler):
    def collect(
        self,
        source: JsonApiSource,
        *,
        days_back: int,
        max_items: int,
    ) -> CollectResult:
        if source.method != "GET":
            raise NotImplementedError(
                f"source {source.id!r}: method={source.method!r} is not yet implemented; "
                "only GET is supported"
            )

        effective_days_back = source.days_back if source.days_back is not None else days_back
        effective_timeout = source.timeout if source.timeout is not None else _DEFAULT_TIMEOUT

        headers: dict[str, str] = {}
        if source.auth_env_var and source.auth_header:
            headers[source.auth_header] = os.environ[source.auth_env_var]

        resp = self.session.get(str(source.url), headers=headers, timeout=effective_timeout)
        resp.raise_for_status()
        data = resp.json()

        raw_list = _resolve_path(data, source.item_path)
        if not isinstance(raw_list, list):
            raise ValueError(
                f"item_path {source.item_path!r} resolved to {type(raw_list).__name__}, expected list"
            )

        fm = source.field_map
        cutoff = datetime.now(timezone.utc) - timedelta(days=effective_days_back)
        now = datetime.now(timezone.utc)
        items: list[RawItem] = []
        date_parse_failures = 0

        for raw in raw_list:
            if len(items) >= max_items:
                break

            url: str = raw.get(fm.get("url", "url"), "") or ""
            if not url:
                continue

            title: str = str(raw.get(fm.get("title", "title"), "(no title)")).strip()
            summary_raw = raw.get(fm.get("body", "body")) or raw.get(fm.get("summary", "summary"))
            summary = str(summary_raw).strip() if summary_raw else None

            pub_raw = raw.get(fm.get("published_at", "published_at"))
            pub, failed = _parse_date(pub_raw)
            if failed:
                date_parse_failures += 1
            if pub is not None and pub < cutoff:
                continue

            items.append(
                RawItem(
                    id=stable_id(source.id, url),
                    title=title,
                    summary=summary,
                    url=url,
                    published_date=pub,
                    date_unknown=pub is None,
                    discovery_date=now,
                    source_name=source.name,
                    source_type="json_api",
                )
            )

        return CollectResult(items=items, date_parse_failures=date_parse_failures)
