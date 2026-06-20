from __future__ import annotations

import copy
import json
import logging
import math
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date
from typing import Any, TypeVar

import anthropic
from pydantic import BaseModel, Field

from monitor_engine.models import (
    AffectedPopulation,
    AnalyzedItem,
    ClientConfig,
    DeepAnalysis,
    DollarAmount,
    EditorialSynthesis,
    EditionAnalysis,
    RawItem,
)
from monitor_engine.analysis.prompts import (
    build_classification_system_prompt,
    build_classification_user_prompt,
    build_deep_analysis_system_prompt,
    build_deep_analysis_user_prompt,
    build_editorial_prompt,
)
from monitor_engine.analysis.validation import (
    normalize_title,
    parse_affected_population,
    parse_dollar_amount,
    validate_factual_claims,
)

logger = logging.getLogger(__name__)

CHEAP_MODEL = "claude-haiku-4-5"
EDITORIAL_MODEL = "claude-sonnet-4-6"

# Per-token pricing (input, output)
_PRICE: dict[str, tuple[float, float]] = {
    CHEAP_MODEL:     (1.00e-6, 5.00e-6),
    EDITORIAL_MODEL: (3.00e-6, 15.00e-6),
}

# Cost-tracking phase labels (kept distinct so the marginal cost of depth is
# visible even though deep analysis shares the cheap model with classification).
PHASE_CLASSIFICATION = "classification"
PHASE_DEEP_ANALYSIS = "deep_analysis"
PHASE_EDITORIAL = "editorial"

DEFAULT_BATCH_SIZE = 8
DEFAULT_MIN_INTERVAL = 1.0   # seconds between consecutive LLM calls (serial mode only)
# Concurrent in-flight LLM requests. The dominant cost of a run is total tokens
# generated serially; issuing batches concurrently is the only thing that cuts
# wall time. The Scorer defaults to 1 (sequential, deterministic) so library
# callers and tests are unaffected; the pipeline opts into real concurrency.
DEFAULT_MAX_CONCURRENCY = 1
PIPELINE_MAX_CONCURRENCY = 4
CLASSIFY_MAX_TOKENS = 4096
EDITORIAL_MAX_TOKENS = 1024

# Deep-analysis per-call max_tokens is sized with headroom relative to the
# expected output of the batch (estimate-per-item × items × headroom), rather
# than a fixed ceiling, so a normal batch sits well under the limit and cannot
# be cut mid-JSON. Tuned against a real healthcare run that measured ~1135
# out-tokens/item and truncated some batches at the old 700×2.0 sizing
# ("Unterminated string" parse failures that wasted a full retry each). The
# estimate now sits above observed output with generous headroom; max_tokens is
# only a ceiling — you pay for tokens actually generated — so over-sizing is cheap.
DEEP_TOKENS_PER_ITEM_ESTIMATE = 1400  # expected deep output per item (obs. ~1135)
DEEP_TOKENS_HEADROOM = 2.5            # multiple of expected output to allow
DEEP_MAX_TOKENS_FLOOR = 2048          # never below this, even for a 1-item batch

_T = TypeVar("_T")


# ─── LLM response shapes ───────────────────────────────────────────────────

class _EditionResult(BaseModel):
    edition_id: str
    relevance_score: int = Field(ge=0, le=100)
    so_what: str
    now_what: str
    categories: list[str]


class _ItemResult(BaseModel):
    item_id: str
    editions: list[_EditionResult]
    dollar_amount_raw: str | None = None
    affected_population_raw: str | None = None
    action_deadline: str | None = None   # YYYY-MM-DD or null
    confidence_note: str | None = None


class _BatchResult(BaseModel):
    results: list[_ItemResult]


class _EditorialResult(BaseModel):
    theme_of_week: str
    editors_note: str
    whats_new_digest: str


# Numeric constraint keywords the structured-output API rejects (Pydantic
# emits them for Field(ge=..., le=...)).  Bounds are still enforced client-
# side when the response is parsed with model_validate_json.
_UNSUPPORTED_NUMERIC_KEYS = (
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
)


def _strict_json_schema(schema: dict) -> dict:
    """
    Make a Pydantic-generated JSON schema acceptable to the structured-output
    API: every object node must carry an explicit ``additionalProperties:
    false``, and integer/number nodes must not carry range constraints.
    Returns a deep copy; the input is not mutated.
    """
    schema = copy.deepcopy(schema)

    def _walk(node: object) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object" or "properties" in node:
                node["additionalProperties"] = False
            if node.get("type") in ("integer", "number"):
                for key in _UNSUPPORTED_NUMERIC_KEYS:
                    node.pop(key, None)
            for value in node.values():
                _walk(value)
        elif isinstance(node, list):
            for value in node:
                _walk(value)

    _walk(schema)
    return schema


# ─── Cost tracking ─────────────────────────────────────────────────────────

@dataclass
class _RunCost:
    # phase label -> {"model": str, "input": int, "output": int}
    _phases: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Guards _phases so cost can be tallied from concurrent worker threads.
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def add(self, phase: str, model: str, usage: Any) -> None:
        with self._lock:
            entry = self._phases.setdefault(phase, {"model": model, "input": 0, "output": 0})
            entry["model"] = model
            entry["input"] += getattr(usage, "input_tokens", 0)
            entry["output"] += getattr(usage, "output_tokens", 0)

    def total_output_tokens(self) -> int:
        with self._lock:
            return sum(e["output"] for e in self._phases.values())

    def _phase_usd(self, entry: dict[str, Any]) -> float:
        inp_rate, out_rate = _PRICE.get(entry["model"], (0.0, 0.0))
        return entry["input"] * inp_rate + entry["output"] * out_rate

    def phase_usd(self, phase: str) -> float:
        entry = self._phases.get(phase)
        return self._phase_usd(entry) if entry else 0.0

    def estimate_usd(self) -> float:
        return sum(self._phase_usd(e) for e in self._phases.values())

    def print_summary(self, *, deep_item_count: int = 0) -> None:
        for phase in (PHASE_CLASSIFICATION, PHASE_DEEP_ANALYSIS, PHASE_EDITORIAL):
            entry = self._phases.get(phase)
            if not entry:
                continue
            cost = self._phase_usd(entry)
            line = (
                f"  {phase:<15} [{entry['model']}]: "
                f"{entry['input']:,} in / {entry['output']:,} out  → ${cost:.4f}"
            )
            if phase == PHASE_DEEP_ANALYSIS and deep_item_count > 0:
                line += (
                    f"  ({entry['output'] / deep_item_count:.0f} out-tok/item, "
                    f"${cost / deep_item_count:.4f}/item over {deep_item_count} items)"
                )
            print(line)
        print(f"  TOTAL ESTIMATED COST: ${self.estimate_usd():.4f}")


# ─── Scorer ────────────────────────────────────────────────────────────────

class Scorer:
    """
    Classify RawItem objects into AnalyzedItem objects using the Anthropic API.

    Usage::

        scorer = Scorer(config)
        items, editorial, cost_usd = scorer.analyze(raw_items)
    """

    def __init__(
        self,
        config: ClientConfig,
        *,
        api_key: str | None = None,
        client: anthropic.Anthropic | None = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
        min_call_interval: float = DEFAULT_MIN_INTERVAL,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
    ) -> None:
        self._config = config
        self._client = client or anthropic.Anthropic(api_key=api_key)
        self._batch_size = batch_size
        self._min_interval = min_call_interval
        self._max_concurrency = max(1, max_concurrency)
        self._last_call: float = 0.0

    # ── internal helpers ───────────────────────────────────────────────────

    def _wait(self) -> None:
        # The min-interval pace only applies in serial mode; under concurrency the
        # worker-pool size is the rate limiter (and a shared _last_call would just
        # re-serialize the calls).
        if self._max_concurrency > 1:
            return
        elapsed = time.monotonic() - self._last_call
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)

    def _run_capped(
        self,
        batches: list,
        fn: Callable[[Any], _T],
        cost: _RunCost,
        caps: Any,
    ) -> list[_T]:
        """Run ``fn`` over ``batches``, honoring the per-run output-token cap.

        Serial when max_concurrency == 1 (cap checked before every batch). When
        concurrent, batches run in waves of max_concurrency with the cap checked
        between waves — so a run may overshoot by at most one wave, which is fine
        for a safety ceiling. Results are returned in submission order either way.
        """
        def _capped() -> bool:
            return cost.total_output_tokens() >= caps.max_output_tokens_per_run

        results: list[_T] = []
        if self._max_concurrency <= 1:
            for batch in batches:
                if _capped():
                    break
                results.append(fn(batch))
            return results

        with ThreadPoolExecutor(max_workers=self._max_concurrency) as pool:
            for start in range(0, len(batches), self._max_concurrency):
                if _capped():
                    break
                wave = batches[start : start + self._max_concurrency]
                results.extend(pool.map(fn, wave))
        return results

    def _make_api_call(
        self,
        model: str,
        system: str,
        user: str,
        schema: dict,
        max_tokens: int,
        *,
        stream: bool = False,
    ) -> tuple[str, Any]:
        """Call the Anthropic API with structured JSON output. Returns (text, usage).

        *schema* must already be API-ready (additionalProperties:false, no numeric
        constraints); callers pass it through _strict_json_schema.

        When *stream* is True the request is streamed and assembled via the SDK's
        stream helper + get_final_message(). Streaming is required for high
        max_tokens / long outputs: a non-streaming request gets a single 600s read
        timeout and can stall on it under server load, whereas a streamed response
        reads incrementally and never hits that single-read window.
        """
        self._wait()
        params = dict(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            # Shape must match the SDK's OutputConfigParam / JSONOutputFormatParam
            # TypedDicts (anthropic/types/) — the API rejects extra keys.
            output_config={
                "format": {
                    "type": "json_schema",
                    "schema": schema,
                }
            },
        )
        if stream:
            with self._client.messages.stream(**params) as s:
                message = s.get_final_message()
        else:
            message = self._client.messages.create(**params)
        self._last_call = time.monotonic()
        return message.content[0].text, message.usage

    def _call_json(
        self,
        phase: str,
        model: str,
        system: str,
        user: str,
        schema: dict,
        max_tokens: int,
        cost: _RunCost,
        parse: Callable[[str], _T],
        *,
        attempts: int = 2,
        stream: bool = False,
    ) -> _T | None:
        """One structured-output call, retried up to *attempts* times.

        Returns the parsed result, or None if every attempt fails (caller decides
        whether that means quarantine, skip, or omit). Tokens are charged to
        *phase* on every attempt that reaches the API. Parsing happens inside the
        retry so a malformed response is retried, not just transport errors.
        Editorial uses attempts=1 (it is best-effort, not load-bearing).
        """
        last_exc: Exception | None = None
        for attempt in range(attempts):
            if attempt > 0:
                time.sleep(self._min_interval)
            try:
                text, usage = self._make_api_call(
                    model, system, user, schema, max_tokens, stream=stream
                )
                cost.add(phase, model, usage)
                return parse(text)
            except Exception as exc:
                last_exc = exc
                logger.warning("%s attempt %d/%d failed: %s", phase, attempt + 1, attempts, exc)
        logger.error("%s: giving up after %d attempt(s): %s", phase, attempts, last_exc)
        return None

    def _classify_batch(
        self,
        batch: list[RawItem],
        system_prompt: str,
        schema: dict,
        cost: _RunCost,
    ) -> list[AnalyzedItem]:
        """Classify one batch; retry once on any failure; quarantine on second failure."""
        parsed = self._call_json(
            PHASE_CLASSIFICATION, CHEAP_MODEL, system_prompt,
            build_classification_user_prompt(batch), schema, CLASSIFY_MAX_TOKENS,
            cost, _BatchResult.model_validate_json,
        )
        if parsed is None:
            logger.error("Quarantining %d items after failed classification", len(batch))
            return []

        raw_by_id = {r.id: r for r in batch}
        results: list[AnalyzedItem] = []
        for llm_item in parsed.results:
            raw = raw_by_id.get(llm_item.item_id)
            if raw is None:
                logger.warning("LLM returned unknown item_id %r — skipping", llm_item.item_id)
                continue
            assembled = self._assemble_item(llm_item, raw)
            if assembled is not None:
                results.append(assembled)
        return results

    def _assemble_item(self, llm: _ItemResult, raw: RawItem) -> AnalyzedItem | None:
        """Convert an LLM result + RawItem into an AnalyzedItem, or None if below threshold."""
        valid_ids = {ed.id for ed in self._config.editions}
        per_edition: dict[str, EditionAnalysis] = {
            e.edition_id: EditionAnalysis(
                relevance_score=e.relevance_score,
                so_what=e.so_what,
                now_what=e.now_what,
                categories=e.categories,
            )
            for e in llm.editions
            if e.edition_id in valid_ids
        }
        if not per_edition:
            return None

        importance = max(e.relevance_score for e in per_edition.values())
        thresholds = self._config.scoring_rubric.thresholds

        if importance >= thresholds.tier_1_min:
            tier: int = 1
        elif importance >= thresholds.tier_2_min:
            tier = 2
        elif importance >= thresholds.tier_3_min:
            tier = 3
        else:
            text = f"{raw.title} {raw.summary or ''}".lower()
            if any(kw.lower() in text for kw in self._config.scoring_rubric.never_discard):
                tier = 3
            else:
                return None  # below threshold, not in never_discard

        dollar_amount: DollarAmount | None = None
        if llm.dollar_amount_raw:
            value, currency = parse_dollar_amount(llm.dollar_amount_raw)
            dollar_amount = DollarAmount(
                raw_text=llm.dollar_amount_raw, value=value, currency=currency
            )

        affected_population: AffectedPopulation | None = None
        if llm.affected_population_raw:
            value, unit = parse_affected_population(llm.affected_population_raw)
            affected_population = AffectedPopulation(
                raw_text=llm.affected_population_raw, value=value, unit=unit
            )

        action_deadline: date | None = None
        if llm.action_deadline:
            try:
                action_deadline = date.fromisoformat(llm.action_deadline)
            except ValueError:
                logger.debug("Could not parse deadline %r for item %r", llm.action_deadline, raw.id)

        clean_title = normalize_title(raw.title)
        item = AnalyzedItem(
            item_id=raw.id,
            title=clean_title,
            raw_title=raw.title if clean_title != raw.title else None,
            url=raw.url,
            source_id=raw.source_name,
            published_at=raw.published_date,
            collected_at=raw.discovery_date,
            tier=tier,
            per_edition=per_edition,
            dollar_amount=dollar_amount,
            affected_population=affected_population,
            action_deadline=action_deadline,
            confidence_note=llm.confidence_note,
        )
        return validate_factual_claims(item, raw)

    # ── deep analysis ───────────────────────────────────────────────────────

    def _deep_analysis_schema(self) -> dict:
        """Build the structured-output schema for a deep-analysis batch from the
        configured sections. Returns an API-ready (strict) schema."""
        da = self._config.deep_analysis
        assert da is not None
        section_props: dict[str, dict] = {}
        for s in da.sections:
            section_props[s.id] = (
                {"type": "array", "items": {"type": "string"}}
                if s.kind == "list" else {"type": "string"}
            )
        item_schema = {
            "type": "object",
            "properties": {
                "item_id": {"type": "string"},
                "sections": {
                    "type": "object",
                    "properties": section_props,
                    "required": list(section_props),
                },
            },
            "required": ["item_id", "sections"],
        }
        schema = {
            "type": "object",
            "properties": {"results": {"type": "array", "items": item_schema}},
            "required": ["results"],
        }
        return _strict_json_schema(schema)

    @staticmethod
    def _deep_max_tokens(n_items: int) -> int:
        """Per-call output ceiling sized with headroom above the batch's expected
        output, so a normal batch sits well under it and cannot be cut mid-JSON."""
        expected = DEEP_TOKENS_PER_ITEM_ESTIMATE * max(1, n_items)
        return max(DEEP_MAX_TOKENS_FLOOR, math.ceil(expected * DEEP_TOKENS_HEADROOM))

    def _deep_analysis_batch(
        self,
        batch: list[RawItem],
        system_prompt: str,
        schema: dict,
        cost: _RunCost,
    ) -> dict[str, DeepAnalysis]:
        """Generate deep analysis for a batch; returns {item_id: DeepAnalysis}.
        Items whose call/parse fails are simply omitted (left without depth).
        Streamed (see _make_api_call) to avoid the non-streaming read-timeout stall."""
        def parse(text: str) -> dict[str, DeepAnalysis]:
            data = json.loads(text)
            out: dict[str, DeepAnalysis] = {}
            for r in data["results"]:
                out[r["item_id"]] = DeepAnalysis.model_validate({"sections": r["sections"]})
            return out

        result = self._call_json(
            PHASE_DEEP_ANALYSIS, CHEAP_MODEL, system_prompt,
            build_deep_analysis_user_prompt(batch), schema, self._deep_max_tokens(len(batch)),
            cost, parse, stream=True,
        )
        if result is None:
            logger.error("Deep analysis failed for %d items — left without depth", len(batch))
            return {}
        return result

    def _editorial_synthesis(
        self, items: list[AnalyzedItem], cost: _RunCost
    ) -> EditorialSynthesis | None:
        system = (
            "You are a senior editor writing concise editorial content. "
            "Respond only with the JSON object requested."
        )
        parsed = self._call_json(
            PHASE_EDITORIAL, EDITORIAL_MODEL, system,
            build_editorial_prompt(items, self._config),
            _strict_json_schema(_EditorialResult.model_json_schema()), EDITORIAL_MAX_TOKENS,
            cost, _EditorialResult.model_validate_json,
            attempts=1,
        )
        if parsed is None:
            return None
        return EditorialSynthesis(
            theme_of_week=parsed.theme_of_week,
            editors_note=parsed.editors_note,
            whats_new_digest=parsed.whats_new_digest,
        )

    # ── public API ─────────────────────────────────────────────────────────

    def _attach_deep_analysis(
        self,
        raw_items: list[RawItem],
        analyzed: list[AnalyzedItem],
        cost: _RunCost,
        caps: Any,
    ) -> tuple[list[AnalyzedItem], int]:
        """Generate deep analysis for every analyzed item (all tiers) and attach
        it. Cap-gated: stops when the per-run output-token cap is reached, leaving
        remaining items without depth. Returns (updated_items, deep_item_count)."""
        system_prompt = build_deep_analysis_system_prompt(self._config)
        schema = self._deep_analysis_schema()
        analyzed_ids = {a.item_id for a in analyzed}
        targets = [r for r in raw_items if r.id in analyzed_ids]   # preserves order

        # Deep batches are sized independently of classification: per-item deep
        # output is much larger, so big batches risk truncation at the ceiling.
        deep_batch_size = self._config.deep_analysis.deep_batch_size

        batches = [targets[i : i + deep_batch_size] for i in range(0, len(targets), deep_batch_size)]
        batch_results = self._run_capped(
            batches,
            lambda b: self._deep_analysis_batch(b, system_prompt, schema, cost),
            cost, caps,
        )
        deep_by_id: dict[str, DeepAnalysis] = {}
        for r in batch_results:
            deep_by_id.update(r)
        if len(batch_results) < len(batches):
            logger.warning(
                "Output token cap reached — stopped after %d of %d deep-analysis batches",
                len(batch_results), len(batches),
            )

        if not deep_by_id:
            return analyzed, 0
        updated = [
            a.model_copy(update={"deep_analysis": deep_by_id[a.item_id]})
            if a.item_id in deep_by_id else a
            for a in analyzed
        ]
        return updated, len(deep_by_id)

    def analyze(
        self,
        items: list[RawItem],
    ) -> tuple[list[AnalyzedItem], EditorialSynthesis | None, float]:
        """
        Classify items, optionally generate per-item deep analysis, and produce
        editorial synthesis.

        Returns (analyzed_items, editorial, estimated_cost_usd).
        Prints a per-phase cost summary to stdout.
        Honors cost_caps from ClientConfig (shared across all phases).
        """
        cost = _RunCost()
        caps = self._config.cost_caps

        if len(items) > caps.max_items_per_run:
            logger.info(
                "Capping from %d to %d items (cost_caps.max_items_per_run)",
                len(items), caps.max_items_per_run,
            )
            items = items[: caps.max_items_per_run]

        classify_schema = _strict_json_schema(_BatchResult.model_json_schema())
        system_prompt = build_classification_system_prompt(self._config)

        batches = [items[i : i + self._batch_size] for i in range(0, len(items), self._batch_size)]
        batch_results = self._run_capped(
            batches,
            lambda b: self._classify_batch(b, system_prompt, classify_schema, cost),
            cost, caps,
        )
        analyzed: list[AnalyzedItem] = [item for br in batch_results for item in br]
        if len(batch_results) < len(batches):
            logger.warning(
                "Output token cap reached — stopped after %d of %d classification batches",
                len(batch_results), len(batches),
            )

        deep_count = 0
        if self._config.deep_analysis is not None and analyzed:
            analyzed, deep_count = self._attach_deep_analysis(items, analyzed, cost, caps)

        editorial: EditorialSynthesis | None = None
        if analyzed:
            editorial = self._editorial_synthesis(analyzed, cost)

        print("\n── Analysis cost estimate ──────────────────────────")
        cost.print_summary(deep_item_count=deep_count)
        print(f"  Items analyzed: {len(analyzed)}  |  with deep analysis: {deep_count}")
        print("────────────────────────────────────────────────────")

        return analyzed, editorial, cost.estimate_usd()
