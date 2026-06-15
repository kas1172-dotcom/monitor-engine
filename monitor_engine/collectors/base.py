from __future__ import annotations

import hashlib
import os
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from monitor_engine.models import ClientConfig, JsonApiSource, RawItem, SourceHealth


_DEFAULT_TIMEOUT = 15  # seconds


def make_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=1.0,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers["User-Agent"] = "monitor-engine/1.0"
    return session


def stable_id(source_id: str, url: str) -> str:
    """Deterministic 16-char hex id for a (source, url) pair."""
    return hashlib.sha256(f"{source_id}:{url}".encode()).hexdigest()[:16]


@dataclass
class CollectResult:
    """Return type for SourceHandler.collect."""
    items: list[RawItem]
    date_parse_failures: int = 0


@dataclass
class CollectionResult:
    """Return type for collect_all: all items plus per-source health data."""
    items: list[RawItem]
    health: dict[str, SourceHealth]


class SourceHandler(ABC):
    def __init__(self, session: requests.Session) -> None:
        self.session = session

    @abstractmethod
    def collect(
        self,
        source: object,
        *,
        days_back: int,
        max_items: int,
    ) -> CollectResult: ...


def check_env_vars(config: ClientConfig) -> None:
    """Fail fast before any network calls if declared env vars are absent."""
    missing = [v for v in config.required_env_vars() if not os.environ.get(v)]
    if missing:
        lines = "\n".join(f"  - {v}" for v in missing)
        raise EnvironmentError(f"Missing required environment variables:\n{lines}")


def collect_all(
    config: ClientConfig,
    *,
    days_back: int = 7,
    max_items_per_source: int | None = None,
) -> CollectionResult:
    """
    Collect from all sources in parallel using a thread pool.

    Per-source failures are captured in health[source_id].error rather than
    propagated as exceptions, so one bad source never aborts the run.
    """
    check_env_vars(config)

    from monitor_engine.collectors.html_list import HtmlListHandler
    from monitor_engine.collectors.json_api import JsonApiHandler
    from monitor_engine.collectors.rss import RssHandler

    session = make_session()
    handler_map: dict[str, SourceHandler] = {
        "rss": RssHandler(session),
        "json_api": JsonApiHandler(session),
        "html_list": HtmlListHandler(session),
    }

    cap = max_items_per_source if max_items_per_source is not None else config.cost_caps.max_items_per_run

    def _one(source) -> tuple[str, CollectResult | Exception]:
        try:
            return source.id, handler_map[source.type].collect(
                source, days_back=days_back, max_items=cap
            )
        except Exception as exc:  # noqa: BLE001
            return source.id, exc

    all_items: list[RawItem] = []
    health: dict[str, SourceHealth] = {}

    with ThreadPoolExecutor(max_workers=min(len(config.sources), 8)) as pool:
        futures = {pool.submit(_one, s): s for s in config.sources}
        for future in as_completed(futures):
            source_id, result = future.result()
            if isinstance(result, Exception):
                health[source_id] = SourceHealth(
                    source_id=source_id,
                    items_collected=0,
                    zero_results=True,
                    error=str(result),
                )
            else:
                all_items.extend(result.items)
                health[source_id] = SourceHealth(
                    source_id=source_id,
                    items_collected=len(result.items),
                    zero_results=len(result.items) == 0,
                    date_parse_failures=result.date_parse_failures,
                )

    return CollectionResult(items=all_items, health=health)
