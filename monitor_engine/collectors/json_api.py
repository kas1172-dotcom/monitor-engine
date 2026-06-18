from __future__ import annotations

import os
import string
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urljoin

from dateutil import parser as dateutil_parser

from monitor_engine.collectors.base import (
    CollectResult,
    SourceHandler,
    per_source_headers,
    stable_id,
    _DEFAULT_TIMEOUT,
)
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


def _build_item_url(raw: dict, source: JsonApiSource) -> str:
    """
    Determine the item URL, in priority order:
      1. url_template — substitute record fields (skip item if any are missing/empty)
      2. mapped url field — resolved against base_url if it's a relative path
    Returns "" when no usable URL can be built (caller skips the item).
    """
    if source.url_template:
        fields = [name for _, name, _, _ in string.Formatter().parse(source.url_template) if name]
        values: dict[str, Any] = {}
        for f in fields:
            v = raw.get(f)
            # Optional per-field value translation (e.g. bill type "HR" → "house-bill").
            if source.url_template_map and f in source.url_template_map:
                v = source.url_template_map[f].get(str(v)) if v is not None else None
            if v in (None, ""):
                return ""   # missing/untranslatable field → can't build a valid URL
            values[f] = v
        try:
            return source.url_template.format(**values)
        except (KeyError, IndexError):
            return ""

    raw_url = raw.get(source.field_map.get("url", "url"), "") or ""
    if raw_url and source.base_url and raw_url.startswith("/"):
        return urljoin(source.base_url, raw_url)
    return raw_url


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
        effective_days_back = source.days_back if source.days_back is not None else days_back
        effective_timeout = source.timeout if source.timeout is not None else _DEFAULT_TIMEOUT

        headers: dict[str, str] = dict(per_source_headers(source))
        if source.auth_env_var and source.auth_header:
            headers[source.auth_header] = os.environ[source.auth_env_var]

        # method is constrained to GET|POST by the schema. POST sends request_body
        # as a JSON payload (the shape most search APIs expect); note make_session
        # only retries GET, so a POST source is not auto-retried on 5xx/429.
        if source.method == "POST":
            resp = self.session.post(
                str(source.url),
                headers=headers,
                json=source.request_body or {},
                timeout=effective_timeout,
            )
        else:
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

            url: str = _build_item_url(raw, source)
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
