"""Tests for the analysis stage. All LLM calls are mocked."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from monitor_engine.analysis.scorer import (
    CHEAP_MODEL,
    DEEP_MAX_TOKENS_FLOOR,
    DEEP_TOKENS_PER_ITEM_ESTIMATE,
    EDITORIAL_MODEL,
    PHASE_CLASSIFICATION,
    PHASE_DEEP_ANALYSIS,
    PHASE_EDITORIAL,
    Scorer,
    _BatchResult,
    _EditorialResult,
    _RunCost,
    _strict_json_schema,
)
from monitor_engine.analysis.prompts import (
    build_classification_system_prompt,
    build_editorial_prompt,
)
from monitor_engine.analysis.validation import (
    normalize_title,
    parse_affected_population,
    parse_dollar_amount,
    validate_factual_claims,
)
from monitor_engine.models import (
    AffectedPopulation,
    AnalyzedItem,
    Branding,
    Cadence,
    ClientConfig,
    CostCaps,
    DeepAnalysisConfig,
    DeepAnalysisSection,
    DollarAmount,
    Edition,
    EditionAnalysis,
    HtmlListSource,
    KeywordPrefilter,
    RawItem,
    RssSource,
    ScoringRubric,
    TierThresholds,
)


# ─── Fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture()
def config() -> ClientConfig:
    return ClientConfig(
        branding=Branding(name="Test Monitor", accent_color="#0066CC"),
        editions=[
            Edition(
                id="exec",
                label="Executive",
                audience_description="C-suite executives tracking strategic trends",
                analysis_instructions="Focus on strategic and financial impact",
                categories=["Finance", "Strategy", "Regulation"],
            ),
            Edition(
                id="ops",
                label="Operations",
                audience_description="Operations teams tracking execution risks",
                analysis_instructions="Focus on operational and supply-chain impact",
                categories=["Supply Chain", "Logistics", "Safety"],
            ),
        ],
        scoring_rubric=ScoringRubric(
            thresholds=TierThresholds(tier_1_min=80, tier_2_min=50, tier_3_min=20),
            never_discard=["emergency", "critical recall"],
        ),
        sources=[
            RssSource(
                type="rss",
                id="test-feed",
                name="Test Feed",
                url="https://example.com/feed",
            )
        ],
        keyword_prefilter=KeywordPrefilter(include=["test"]),
        cadence=Cadence(cron="0 6 * * 1"),
        cost_caps=CostCaps(max_items_per_run=20, max_output_tokens_per_run=10_000),
    )


def _raw(
    item_id: str = "item-001",
    title: str = "Test Article Title",
    summary: str | None = "Summary text about budget of $1.2 billion for 50,000 personnel.",
) -> RawItem:
    return RawItem(
        id=item_id,
        title=title,
        summary=summary,
        url=f"https://example.com/{item_id}",
        published_date=datetime(2026, 6, 10, tzinfo=timezone.utc),
        date_unknown=False,
        discovery_date=datetime(2026, 6, 11, tzinfo=timezone.utc),
        source_name="Test Feed",
        source_type="rss",
    )


def _mock_response(payload: dict, input_tokens: int = 100, output_tokens: int = 200) -> MagicMock:
    usage = MagicMock()
    usage.input_tokens = input_tokens
    usage.output_tokens = output_tokens

    content = MagicMock()
    content.text = json.dumps(payload)

    response = MagicMock()
    response.content = [content]
    response.usage = usage
    return response


def _batch_payload(item_id: str, score: int = 85) -> dict:
    return {
        "results": [
            {
                "item_id": item_id,
                "editions": [
                    {
                        "edition_id": "exec",
                        "relevance_score": score,
                        "so_what": "This matters strategically.",
                        "now_what": "Review budget implications.",
                        "categories": ["Finance"],
                    }
                ],
                "dollar_amount_raw": "$1.2 billion",
                "affected_population_raw": "50,000 personnel",
                "action_deadline": None,
                "confidence_note": None,
            }
        ]
    }


def _editorial_payload() -> dict:
    return {
        "theme_of_week": "Budgetary pressure across the sector.",
        "editors_note": "Three major programs saw funding shifts this week.",
        "whats_new_digest": "New allocations signal a pivot toward modernization.",
    }


# ─── Title normalization ──────────────────────────────────────────────────

class TestNormalizeTitle:
    def test_recall_product_dump_is_cut_and_descreamed(self):
        # Real FDA recall feed title.
        raw = ("TopCare health, ULTRA STRENGTH, Antacid Tablets, CALCIUM CARBONATE "
               "1000mg, 72 CHEWABLE TABLETS, DISTRIBUTED BY TOPCO ASSOCIATES LLC.,ELK GROVE")
        out = normalize_title(raw)
        assert "DISTRIBUTED BY" not in out          # boilerplate tail cut
        assert "Calcium Carbonate" in out           # de-SCREAMed
        assert "CARBONATE" not in out
        assert len(out) <= 81                        # capped (+1 for the ellipsis char)

    def test_lidocaine_cut_at_rx_only(self):
        raw = ("Lidocaine HCl Injection USP, 25x5 mL, Single-Dose Ampules, Rx Only, "
               "Distributed by: Spectra Medical Devices, LLC, Wilmington, Made in S. Korea")
        out = normalize_title(raw)
        assert out == "Lidocaine HCl Injection USP, 25x5 mL, Single-Dose Ampules"
        assert "HCl" in out and "USP" in out         # real acronyms preserved

    def test_long_clean_headline_only_capped(self):
        raw = ("Federal Medicaid Spending Through State Directed Payments Nears $100 "
               "Billion Annually Across 41 States, With New Limits Set to Reduce Funding")
        out = normalize_title(raw)
        assert out.startswith("Federal Medicaid Spending")
        assert out.endswith("…")
        assert "$100 Billion" in out                 # dollar figure kept, not mangled

    def test_short_clean_title_unchanged(self):
        assert normalize_title("FDA approves new infusion device") == "FDA approves new infusion device"

    def test_hyphenated_words_preserved(self):
        raw = "Build-to-Print Manufacturing Expands at AS9100 Facility"
        assert normalize_title(raw) == raw           # no clipping of Build-to-Print, AS9100 kept

    def test_money_suffix_not_descreamed(self):
        assert "$40M" in normalize_title("Ahold Delhaize USA Inc. to Pay $40M for Inflated Drug Prices")

    def test_empty_and_whitespace(self):
        assert normalize_title("") == ""
        assert normalize_title("   Spaced   out    title  ") == "Spaced out title"


# ─── Parser tests ─────────────────────────────────────────────────────────

class TestParseDollarAmount:
    def test_simple_usd_symbol(self):
        value, currency = parse_dollar_amount("$1.2 billion")
        assert value == pytest.approx(1.2e9)
        assert currency == "USD"

    def test_millions(self):
        value, currency = parse_dollar_amount("€500 million")
        assert value == pytest.approx(500e6)
        assert currency == "EUR"

    def test_thousands_with_comma(self):
        value, _ = parse_dollar_amount("$1,500")
        assert value == pytest.approx(1500)

    def test_iso_code(self):
        _, currency = parse_dollar_amount("1.5 billion USD")
        assert currency == "USD"

    def test_empty_returns_none(self):
        assert parse_dollar_amount("") == (None, None)
        assert parse_dollar_amount("no numbers here") == (None, None)

    def test_k_suffix(self):
        value, _ = parse_dollar_amount("$250k")
        assert value == pytest.approx(250_000)


class TestParseAffectedPopulation:
    def test_with_unit(self):
        value, unit = parse_affected_population("50,000 personnel")
        assert value == 50_000
        assert unit == "personnel"

    def test_millions(self):
        value, unit = parse_affected_population("3 million residents")
        assert value == 3_000_000
        assert unit == "residents"

    def test_no_unit(self):
        value, unit = parse_affected_population("12,000")
        assert value == 12_000

    def test_empty(self):
        assert parse_affected_population("") == (None, None)


# ─── Factual validation tests ─────────────────────────────────────────────

def _analyzed(
    dollar_raw: str | None = None,
    dollar_val: float | None = None,
    pop_raw: str | None = None,
    pop_val: int | None = None,
    deadline: str | None = None,
) -> AnalyzedItem:
    from datetime import date

    return AnalyzedItem(
        item_id="x",
        title="Test",
        url="https://example.com",
        source_id="test",
        published_at=None,
        collected_at=datetime(2026, 6, 11, tzinfo=timezone.utc),
        tier=2,
        per_edition={
            "exec": EditionAnalysis(
                relevance_score=60,
                so_what="matters",
                now_what="act",
                categories=[],
            )
        },
        dollar_amount=DollarAmount(raw_text=dollar_raw, value=dollar_val) if dollar_raw else None,
        affected_population=(
            AffectedPopulation(raw_text=pop_raw, value=pop_val) if pop_raw else None
        ),
        action_deadline=date.fromisoformat(deadline) if deadline else None,
    )


class TestValidateFactualClaims:
    def test_dollar_amount_found(self):
        item = _analyzed(dollar_raw="$1.2 billion", dollar_val=1.2e9)
        raw = _raw(summary="Budget of $1.2 billion approved.")
        result = validate_factual_claims(item, raw)
        assert result.unverified_claims == []
        assert result.confidence_note is None

    def test_dollar_amount_not_found_flags(self):
        item = _analyzed(dollar_raw="$999 billion", dollar_val=999e9)
        raw = _raw(summary="Small contract worth $50 million.")
        result = validate_factual_claims(item, raw)
        assert len(result.unverified_claims) == 1
        assert "dollar_amount" in result.unverified_claims[0]
        assert "UNVERIFIED" in (result.confidence_note or "")

    def test_deadline_found(self):
        item = _analyzed(deadline="2026-12-31")
        raw = _raw(summary="Must act by 2026-12-31.")
        result = validate_factual_claims(item, raw)
        assert result.unverified_claims == []

    def test_deadline_not_found_flags(self):
        item = _analyzed(deadline="2026-03-15")
        raw = _raw(summary="No specific deadline mentioned.")
        result = validate_factual_claims(item, raw)
        assert any("action_deadline" in c for c in result.unverified_claims)

    def test_no_claims_no_change(self):
        item = _analyzed()
        raw = _raw()
        result = validate_factual_claims(item, raw)
        assert result.unverified_claims == []
        assert result.confidence_note is None

    # ── Currency-format equivalence (regression: "$40M" vs source) ──────────
    # The extracted value is canonical (40,000,000); the source token may be
    # glued ("$40M"), spaced ("$40 million"), or comma-grouped ("$40,000,000").
    # All must verify against an extracted value of 40,000,000.
    @pytest.mark.parametrize("source_phrase", [
        "Acme to Pay $40M for violations",
        "Acme to Pay $40 million for violations",
        "Acme to Pay $40,000,000 for violations",
        "Acme to Pay $40.0 million for violations",
    ])
    def test_dollar_glued_and_spaced_forms_all_verify(self, source_phrase):
        item = _analyzed(dollar_raw="$40M", dollar_val=40_000_000)
        result = validate_factual_claims(item, _raw(title=source_phrase, summary=None))
        assert result.unverified_claims == []
        assert result.confidence_note is None

    def test_dollar_genuinely_absent_still_flags(self):
        item = _analyzed(dollar_raw="$40M", dollar_val=40_000_000)
        result = validate_factual_claims(
            item, _raw(title="Acme settles case", summary="No dollar figure disclosed.")
        )
        assert any("dollar_amount" in c for c in result.unverified_claims)

    def test_population_glued_multiplier_verifies(self):
        # 2,000,000 extracted vs glued "2M" in source — same matcher as dollars
        item = _analyzed(pop_raw="2M beneficiaries", pop_val=2_000_000)
        result = validate_factual_claims(
            item, _raw(title="Rule affects 2M beneficiaries", summary=None)
        )
        assert result.unverified_claims == []

    def test_population_genuinely_absent_flags(self):
        item = _analyzed(pop_raw="2M beneficiaries", pop_val=2_000_000)
        result = validate_factual_claims(
            item, _raw(title="Rule issued", summary="No population figure given.")
        )
        assert any("affected_population" in c for c in result.unverified_claims)


# ─── Scorer integration tests (mocked LLM) ────────────────────────────────

class TestScorerAnalyze:
    def test_successful_single_item(self, config: ClientConfig):
        raw = _raw()
        mock_client = MagicMock()
        mock_client.messages.create.return_value = _mock_response(_batch_payload(raw.id))

        scorer = Scorer(config, client=mock_client, min_call_interval=0)
        analyzed, editorial, cost = scorer.analyze([raw])

        assert len(analyzed) == 1
        assert analyzed[0].tier == 1          # score 85 → tier 1
        assert analyzed[0].item_id == raw.id
        assert analyzed[0].dollar_amount is not None
        assert analyzed[0].dollar_amount.value == pytest.approx(1.2e9)
        assert analyzed[0].affected_population is not None
        assert analyzed[0].affected_population.value == 50_000

    def test_tier_assignment_tier2(self, config: ClientConfig):
        raw = _raw()
        mock_client = MagicMock()
        mock_client.messages.create.return_value = _mock_response(
            _batch_payload(raw.id, score=65)
        )

        scorer = Scorer(config, client=mock_client, min_call_interval=0)
        analyzed, _, _ = scorer.analyze([raw])

        assert analyzed[0].tier == 2

    def test_below_threshold_discarded(self, config: ClientConfig):
        raw = _raw(title="Irrelevant item", summary="Nothing of interest here.")
        payload = {
            "results": [
                {
                    "item_id": raw.id,
                    "editions": [
                        {
                            "edition_id": "exec",
                            "relevance_score": 10,
                            "so_what": "Low relevance.",
                            "now_what": "No action.",
                            "categories": [],
                        }
                    ],
                    "dollar_amount_raw": None,
                    "affected_population_raw": None,
                    "action_deadline": None,
                    "confidence_note": None,
                }
            ]
        }
        mock_client = MagicMock()
        mock_client.messages.create.return_value = _mock_response(payload)

        scorer = Scorer(config, client=mock_client, min_call_interval=0)
        analyzed, _, _ = scorer.analyze([raw])

        assert analyzed == []

    def test_never_discard_keyword_overrides_threshold(self, config: ClientConfig):
        raw = _raw(title="Emergency recall issued", summary="critical recall of 500 units.")
        payload = {
            "results": [
                {
                    "item_id": raw.id,
                    "editions": [
                        {
                            "edition_id": "exec",
                            "relevance_score": 10,
                            "so_what": "Matches never_discard keyword.",
                            "now_what": "Escalate.",
                            "categories": [],
                        }
                    ],
                    "dollar_amount_raw": None,
                    "affected_population_raw": None,
                    "action_deadline": None,
                    "confidence_note": None,
                }
            ]
        }
        mock_client = MagicMock()
        mock_client.messages.create.return_value = _mock_response(payload)

        scorer = Scorer(config, client=mock_client, min_call_interval=0)
        analyzed, _, _ = scorer.analyze([raw])

        assert len(analyzed) == 1
        assert analyzed[0].tier == 3

    def test_retry_on_first_failure_then_success(self, config: ClientConfig):
        raw = _raw()
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = [
            Exception("transient API error"),
            _mock_response(_batch_payload(raw.id)),
            _mock_response(_editorial_payload()),  # editorial call
        ]

        scorer = Scorer(config, client=mock_client, min_call_interval=0)
        analyzed, _, _ = scorer.analyze([raw])

        assert len(analyzed) == 1

    def test_quarantine_after_two_failures(self, config: ClientConfig):
        raw = _raw()
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = [
            Exception("fail 1"),
            Exception("fail 2"),
        ]

        scorer = Scorer(config, client=mock_client, min_call_interval=0)
        analyzed, editorial, _ = scorer.analyze([raw])

        assert analyzed == []
        assert editorial is None

    def test_cost_cap_max_items_respected(self, config: ClientConfig):
        config.cost_caps.max_items_per_run = 2
        items = [_raw(f"item-{i:03d}") for i in range(5)]

        call_count = 0

        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            # editorial call returns editorial, classification calls return batch result
            if call_count == 1:
                # Only 2 items in the batch due to cap
                item_ids = [it.id for it in items[:2]]
                return _mock_response({
                    "results": [
                        {
                            "item_id": iid,
                            "editions": [
                                {
                                    "edition_id": "exec",
                                    "relevance_score": 85,
                                    "so_what": "matters",
                                    "now_what": "act",
                                    "categories": [],
                                }
                            ],
                            "dollar_amount_raw": None,
                            "affected_population_raw": None,
                            "action_deadline": None,
                            "confidence_note": None,
                        }
                        for iid in item_ids
                    ]
                })
            return _mock_response(_editorial_payload())

        mock_client = MagicMock()
        mock_client.messages.create.side_effect = side_effect

        scorer = Scorer(config, client=mock_client, min_call_interval=0)
        analyzed, _, _ = scorer.analyze(items)

        assert len(analyzed) == 2

    def test_output_token_cap_stops_batching(self, config: ClientConfig):
        config.cost_caps.max_output_tokens_per_run = 100
        items = [_raw(f"item-{i:03d}") for i in range(10)]

        call_count = 0

        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            # Return high token usage so cap is hit after first batch
            batch_payload = _batch_payload(f"item-{(call_count - 1) * 8:03d}")
            return _mock_response(batch_payload, output_tokens=200)

        mock_client = MagicMock()
        mock_client.messages.create.side_effect = side_effect

        scorer = Scorer(config, client=mock_client, batch_size=3, min_call_interval=0)
        analyzed, _, _ = scorer.analyze(items)

        # After first batch (200 tokens > 100 cap), second batch should not run
        assert mock_client.messages.create.call_count <= 2  # 1 batch + maybe editorial

    def test_cost_estimate_returned(self, config: ClientConfig):
        raw = _raw()
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = [
            _mock_response(_batch_payload(raw.id), input_tokens=500, output_tokens=300),
            _mock_response(_editorial_payload(), input_tokens=200, output_tokens=100),
        ]

        scorer = Scorer(config, client=mock_client, min_call_interval=0)
        _, _, cost_usd = scorer.analyze([raw])

        # haiku: 500*1e-6 + 300*5e-6 = 0.0005 + 0.0015 = 0.002
        # sonnet: 200*3e-6 + 100*15e-6 = 0.0006 + 0.0015 = 0.0021
        assert cost_usd == pytest.approx(0.002 + 0.0021, rel=0.01)

    def test_editorial_synthesis_on_success(self, config: ClientConfig):
        raw = _raw()
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = [
            _mock_response(_batch_payload(raw.id)),
            _mock_response(_editorial_payload()),
        ]

        scorer = Scorer(config, client=mock_client, min_call_interval=0)
        _, editorial, _ = scorer.analyze([raw])

        assert editorial is not None
        assert editorial.theme_of_week == "Budgetary pressure across the sector."

    def test_editorial_failure_does_not_crash(self, config: ClientConfig):
        raw = _raw()
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = [
            _mock_response(_batch_payload(raw.id)),
            Exception("editorial API error"),
        ]

        scorer = Scorer(config, client=mock_client, min_call_interval=0)
        analyzed, editorial, _ = scorer.analyze([raw])

        assert len(analyzed) == 1
        assert editorial is None

    def test_unknown_edition_id_filtered(self, config: ClientConfig):
        raw = _raw()
        payload = {
            "results": [
                {
                    "item_id": raw.id,
                    "editions": [
                        {
                            "edition_id": "nonexistent_edition",
                            "relevance_score": 90,
                            "so_what": "matters",
                            "now_what": "act",
                            "categories": [],
                        }
                    ],
                    "dollar_amount_raw": None,
                    "affected_population_raw": None,
                    "action_deadline": None,
                    "confidence_note": None,
                }
            ]
        }
        mock_client = MagicMock()
        mock_client.messages.create.return_value = _mock_response(payload)

        scorer = Scorer(config, client=mock_client, min_call_interval=0)
        analyzed, _, _ = scorer.analyze([raw])

        # Item discarded because per_edition is empty after filtering unknown edition
        assert analyzed == []

    def test_importance_score_is_max_across_editions(self, config: ClientConfig):
        raw = _raw()
        payload = {
            "results": [
                {
                    "item_id": raw.id,
                    "editions": [
                        {
                            "edition_id": "exec",
                            "relevance_score": 60,
                            "so_what": "exec matters",
                            "now_what": "exec act",
                            "categories": ["Finance"],
                        },
                        {
                            "edition_id": "ops",
                            "relevance_score": 82,
                            "so_what": "ops matters",
                            "now_what": "ops act",
                            "categories": ["Safety"],
                        },
                    ],
                    "dollar_amount_raw": None,
                    "affected_population_raw": None,
                    "action_deadline": None,
                    "confidence_note": None,
                }
            ]
        }
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = [
            _mock_response(payload),
            _mock_response(_editorial_payload()),
        ]

        scorer = Scorer(config, client=mock_client, min_call_interval=0)
        analyzed, _, _ = scorer.analyze([raw])

        assert len(analyzed) == 1
        assert analyzed[0].importance_score == 82   # max of 60, 82
        assert analyzed[0].tier == 1                # 82 >= tier_1_min (80)

    def test_batching_multiple_items(self, config: ClientConfig):
        items = [_raw(f"item-{i:03d}") for i in range(4)]

        def make_payload(batch_items):
            return {
                "results": [
                    {
                        "item_id": it.id,
                        "editions": [
                            {
                                "edition_id": "exec",
                                "relevance_score": 70,
                                "so_what": "matters",
                                "now_what": "act",
                                "categories": [],
                            }
                        ],
                        "dollar_amount_raw": None,
                        "affected_population_raw": None,
                        "action_deadline": None,
                        "confidence_note": None,
                    }
                    for it in batch_items
                ]
            }

        mock_client = MagicMock()
        # batch_size=3 → two classification calls + one editorial
        mock_client.messages.create.side_effect = [
            _mock_response(make_payload(items[:3])),
            _mock_response(make_payload(items[3:])),
            _mock_response(_editorial_payload()),
        ]

        scorer = Scorer(config, client=mock_client, batch_size=3, min_call_interval=0)
        analyzed, _, _ = scorer.analyze(items)

        assert len(analyzed) == 4
        # Two classification calls + one editorial = 3 total
        assert mock_client.messages.create.call_count == 3

    def test_action_deadline_parsed(self, config: ClientConfig):
        from datetime import date

        raw = _raw(summary="Deadline is 2026-09-30 for submissions.")
        payload = {
            "results": [
                {
                    "item_id": raw.id,
                    "editions": [
                        {
                            "edition_id": "exec",
                            "relevance_score": 75,
                            "so_what": "matters",
                            "now_what": "submit before deadline",
                            "categories": [],
                        }
                    ],
                    "dollar_amount_raw": None,
                    "affected_population_raw": None,
                    "action_deadline": "2026-09-30",
                    "confidence_note": None,
                }
            ]
        }
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = [
            _mock_response(payload),
            _mock_response(_editorial_payload()),
        ]

        scorer = Scorer(config, client=mock_client, min_call_interval=0)
        analyzed, _, _ = scorer.analyze([raw])

        assert analyzed[0].action_deadline == date(2026, 9, 30)
        # "2026" is in the source text, so deadline should be verified
        assert analyzed[0].unverified_claims == []


# ─── Structured-output schema strictness ──────────────────────────────────
# The API rejects any object node lacking an explicit additionalProperties:
# false, and Pydantic's model_json_schema() omits it entirely.

def _object_nodes_missing_strict(node: object, path: str = "$") -> list[str]:
    """Walk a JSON schema and return paths of object nodes where
    additionalProperties is not exactly False."""
    missing: list[str] = []
    if isinstance(node, dict):
        if node.get("type") == "object" or "properties" in node:
            if node.get("additionalProperties") is not False:
                missing.append(path)
        for key, value in node.items():
            missing += _object_nodes_missing_strict(value, f"{path}.{key}")
    elif isinstance(node, list):
        for i, value in enumerate(node):
            missing += _object_nodes_missing_strict(value, f"{path}[{i}]")
    return missing


_NUMERIC_CONSTRAINT_KEYS = {
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
}


def _numeric_constraint_paths(node: object, path: str = "$") -> list[str]:
    """Walk a JSON schema and return paths of numeric nodes carrying range
    constraints the structured-output API rejects."""
    found: list[str] = []
    if isinstance(node, dict):
        if node.get("type") in ("integer", "number"):
            for key in _NUMERIC_CONSTRAINT_KEYS & node.keys():
                found.append(f"{path}.{key}")
        for key, value in node.items():
            found += _numeric_constraint_paths(value, f"{path}.{key}")
    elif isinstance(node, list):
        for i, value in enumerate(node):
            found += _numeric_constraint_paths(value, f"{path}[{i}]")
    return found


class TestStrictJsonSchema:
    def test_raw_pydantic_schema_is_not_strict(self):
        # Sanity check: without post-processing the schema would be rejected
        assert _object_nodes_missing_strict(_BatchResult.model_json_schema())

    def test_raw_pydantic_schema_has_numeric_constraints(self):
        # Sanity check: Field(ge=0, le=100) emits minimum/maximum, which the
        # API rejects — proves the stripping test below is meaningful.
        assert _numeric_constraint_paths(_BatchResult.model_json_schema())

    def test_numeric_constraints_stripped(self):
        schema = _strict_json_schema(_BatchResult.model_json_schema())
        assert _numeric_constraint_paths(schema) == []

    def test_relevance_score_bounds_still_enforced_client_side(self):
        # Stripping the schema must not weaken response validation
        bad = _batch_payload("item-001", score=150)
        with pytest.raises(Exception):
            _BatchResult.model_validate_json(json.dumps(bad))

    def test_batch_result_schema_all_objects_strict(self):
        schema = _strict_json_schema(_BatchResult.model_json_schema())
        assert _object_nodes_missing_strict(schema) == []

    def test_editorial_result_schema_all_objects_strict(self):
        schema = _strict_json_schema(_EditorialResult.model_json_schema())
        assert _object_nodes_missing_strict(schema) == []

    def test_nested_defs_are_strict(self):
        schema = _strict_json_schema(_BatchResult.model_json_schema())
        for name, definition in schema.get("$defs", {}).items():
            assert definition.get("additionalProperties") is False, name

    def test_input_schema_not_mutated(self):
        original = _BatchResult.model_json_schema()
        snapshot = json.dumps(original, sort_keys=True)
        _strict_json_schema(original)
        assert json.dumps(original, sort_keys=True) == snapshot

    def test_properties_and_required_preserved(self):
        original = _BatchResult.model_json_schema()
        strict = _strict_json_schema(original)
        assert strict["properties"].keys() == original["properties"].keys()
        assert strict["required"] == original["required"]

    def test_schemas_sent_to_api_are_strict(self, config):
        # End-to-end: walk the schema dicts actually passed to messages.create
        # for both the classification and editorial calls.
        client = MagicMock()
        client.messages.create.side_effect = [
            _mock_response(_batch_payload("item-001")),
            _mock_response(_editorial_payload()),
        ]
        scorer = Scorer(config, client=client, min_call_interval=0)
        scorer.analyze([_raw()])

        assert client.messages.create.call_count == 2
        for call in client.messages.create.call_args_list:
            sent = call.kwargs["output_config"]["format"]["schema"]
            assert _object_nodes_missing_strict(sent) == []
            assert _numeric_constraint_paths(sent) == []

    def test_request_payload_matches_sdk_types(self, config):
        # The API rejects keys the SDK's TypedDicts don't define (e.g. the
        # 'name' key once sent inside format).  Derive the permitted key sets
        # from the installed SDK so this test tracks SDK upgrades.
        from anthropic.types.json_output_format_param import JSONOutputFormatParam
        from anthropic.types.output_config_param import OutputConfigParam
        from anthropic.types.message_create_params import MessageCreateParamsNonStreaming

        def _keys(td: type) -> frozenset[str]:
            return td.__required_keys__ | td.__optional_keys__

        client = MagicMock()
        client.messages.create.side_effect = [
            _mock_response(_batch_payload("item-001")),
            _mock_response(_editorial_payload()),
        ]
        scorer = Scorer(config, client=client, min_call_interval=0)
        scorer.analyze([_raw()])

        for call in client.messages.create.call_args_list:
            extra_top = set(call.kwargs) - _keys(MessageCreateParamsNonStreaming)
            assert not extra_top, f"extra top-level keys: {extra_top}"

            output_config = call.kwargs["output_config"]
            extra_cfg = set(output_config) - _keys(OutputConfigParam)
            assert not extra_cfg, f"extra output_config keys: {extra_cfg}"

            fmt = output_config["format"]
            extra_fmt = set(fmt) - _keys(JSONOutputFormatParam)
            assert not extra_fmt, f"extra format keys: {extra_fmt}"
            # Required keys must also be present
            assert fmt["type"] == "json_schema"
            assert isinstance(fmt["schema"], dict)


# ─── Deep analysis (config-driven, per item, all tiers) ────────────────────

_DEEP_SECTIONS = [
    DeepAnalysisSection(id="background", label="Background", kind="text",
                        instruction="Give two sentences of grounding context."),
    DeepAnalysisSection(id="risks", label="Risks", kind="list",
                        instruction="List the salient risks."),
]


def _deep_config(config: ClientConfig) -> ClientConfig:
    config.deep_analysis = DeepAnalysisConfig(
        instruction="Write in-depth, source-grounded analysis.",
        sections=_DEEP_SECTIONS,
    )
    return config


def _deep_payload(*item_ids: str) -> dict:
    return {
        "results": [
            {
                "item_id": iid,
                "sections": {
                    "background": f"Context for {iid}.",
                    "risks": [f"Risk A for {iid}", f"Risk B for {iid}"],
                },
            }
            for iid in item_ids
        ]
    }


def _classify_payload(item_id: str, score: int) -> dict:
    return {
        "results": [
            {
                "item_id": item_id,
                "editions": [
                    {
                        "edition_id": "exec",
                        "relevance_score": score,
                        "so_what": "matters",
                        "now_what": "act",
                        "categories": [],
                    }
                ],
                "dollar_amount_raw": None,
                "affected_population_raw": None,
                "action_deadline": None,
                "confidence_note": None,
            }
            for _ in [0]
        ]
    }


def _mock_stream(payload: dict, input_tokens: int = 300, output_tokens: int = 600) -> MagicMock:
    """Mock the SDK stream helper: `with client.messages.stream(...) as s:
    s.get_final_message()`. Returns a context-manager mock whose
    get_final_message() yields a response with .content[0].text and .usage."""
    message = _mock_response(payload, input_tokens, output_tokens)
    cm = MagicMock()
    cm.__enter__.return_value.get_final_message.return_value = message
    cm.__exit__.return_value = False
    return cm


def _multi_classify(*pairs: tuple[str, int]) -> dict:
    """One classification batch payload covering several (item_id, score) items."""
    results = []
    for iid, score in pairs:
        results += _classify_payload(iid, score)["results"]
    return {"results": results}


class TestDeepAnalysis:
    def test_attached_to_all_tiers_including_tier3(self, config):
        cfg = _deep_config(config)
        cfg.scoring_rubric.never_discard = []
        items = [_raw("item-001"), _raw("item-002"), _raw("item-003")]
        client = MagicMock()
        # classify (create) covers all 3 → tiers 1/2/3; editorial (create)
        client.messages.create.side_effect = [
            _mock_response(_multi_classify(("item-001", 85), ("item-002", 65), ("item-003", 30))),
            _mock_response(_editorial_payload()),
        ]
        # deep (stream): deep_batch_size default 3 → one streamed call for all 3
        client.messages.stream.side_effect = [
            _mock_stream(_deep_payload("item-001", "item-002", "item-003")),
        ]
        scorer = Scorer(cfg, client=client, min_call_interval=0)
        analyzed, _, _ = scorer.analyze(items)

        by_tier = {a.tier: a for a in analyzed}
        assert set(by_tier) == {1, 2, 3}
        for a in analyzed:
            assert a.deep_analysis is not None, f"tier {a.tier} missing deep analysis"
            assert a.deep_analysis.sections["background"].startswith("Context for")
            assert isinstance(a.deep_analysis.sections["risks"], list)

    def test_no_deep_config_makes_no_deep_call(self, config):
        # config fixture has deep_analysis=None → classify + editorial only, no stream.
        client = MagicMock()
        client.messages.create.side_effect = [
            _mock_response(_batch_payload("item-001")),
            _mock_response(_editorial_payload()),
        ]
        scorer = Scorer(config, client=client, min_call_interval=0)
        analyzed, _, _ = scorer.analyze([_raw()])
        assert client.messages.create.call_count == 2
        client.messages.stream.assert_not_called()
        assert analyzed[0].deep_analysis is None

    def test_deep_schema_is_strict_and_has_section_keys(self, config):
        scorer = Scorer(_deep_config(config), client=MagicMock(), min_call_interval=0)
        schema = scorer._deep_analysis_schema()
        assert _object_nodes_missing_strict(schema) == []
        assert _numeric_constraint_paths(schema) == []
        sections = (
            schema["properties"]["results"]["items"]
            ["properties"]["sections"]["properties"]
        )
        assert set(sections) == {"background", "risks"}
        assert sections["risks"]["type"] == "array"
        assert sections["background"]["type"] == "string"

    def test_deep_schema_sent_to_api_is_strict(self, config):
        cfg = _deep_config(config)
        cfg.scoring_rubric.never_discard = []
        client = MagicMock()
        client.messages.create.side_effect = [
            _mock_response(_classify_payload("item-001", 85)),
            _mock_response(_editorial_payload()),
        ]
        client.messages.stream.side_effect = [_mock_stream(_deep_payload("item-001"))]
        scorer = Scorer(cfg, client=client, min_call_interval=0)
        scorer.analyze([_raw("item-001")])

        # classify + editorial via create (2), deep via stream (1) — all schemas strict
        assert client.messages.create.call_count == 2
        assert client.messages.stream.call_count == 1
        for call in (*client.messages.create.call_args_list, *client.messages.stream.call_args_list):
            sent = call.kwargs["output_config"]["format"]["schema"]
            assert _object_nodes_missing_strict(sent) == []
            assert _numeric_constraint_paths(sent) == []

    def test_deep_calls_are_streamed_not_created(self, config):
        # Item 2: deep analysis must go through the stream helper, never create.
        cfg = _deep_config(config)
        cfg.scoring_rubric.never_discard = []
        client = MagicMock()
        client.messages.create.side_effect = [
            _mock_response(_classify_payload("item-001", 85)),
            _mock_response(_editorial_payload()),
        ]
        client.messages.stream.side_effect = [_mock_stream(_deep_payload("item-001"))]
        scorer = Scorer(cfg, client=client, min_call_interval=0)
        analyzed, _, _ = scorer.analyze([_raw("item-001")])

        client.messages.stream.assert_called_once()
        da = analyzed[0].deep_analysis
        assert da is not None
        assert da.sections["background"] == "Context for item-001."
        assert da.sections["risks"] == ["Risk A for item-001", "Risk B for item-001"]

    def test_deep_batch_size_independent_of_classification(self, config):
        # deep_batch_size=2 with 5 items → 1 classify call (batch_size 8) but
        # 3 deep stream calls (ceil(5/2)). Proves the two batch sizes are decoupled.
        cfg = _deep_config(config)
        cfg.deep_analysis.deep_batch_size = 2
        cfg.scoring_rubric.never_discard = []
        ids = [f"item-{i:03d}" for i in range(1, 6)]
        client = MagicMock()
        client.messages.create.side_effect = [
            _mock_response(_multi_classify(*[(i, 70) for i in ids])),
            _mock_response(_editorial_payload()),
        ]
        client.messages.stream.side_effect = [
            _mock_stream(_deep_payload(ids[0], ids[1])),
            _mock_stream(_deep_payload(ids[2], ids[3])),
            _mock_stream(_deep_payload(ids[4])),
        ]
        scorer = Scorer(cfg, client=client, batch_size=8, min_call_interval=0)
        analyzed, _, _ = scorer.analyze([_raw(i) for i in ids])

        assert client.messages.create.call_count == 2     # 1 classify + 1 editorial
        assert client.messages.stream.call_count == 3      # ceil(5 / deep_batch_size=2)
        assert all(a.deep_analysis is not None for a in analyzed)

    def test_deep_max_tokens_scales_with_batch_and_has_floor(self, config):
        # Unit-level: per-call ceiling grows with batch size, never below the floor.
        assert Scorer._deep_max_tokens(1) >= DEEP_MAX_TOKENS_FLOOR
        big = Scorer._deep_max_tokens(3)
        assert big >= 3 * DEEP_TOKENS_PER_ITEM_ESTIMATE          # headroom above expected
        assert Scorer._deep_max_tokens(6) > big                  # scales with batch

    def test_deep_max_tokens_sent_to_stream_has_headroom(self, config):
        cfg = _deep_config(config)
        cfg.deep_analysis.deep_batch_size = 3
        cfg.scoring_rubric.never_discard = []
        ids = [f"item-{i:03d}" for i in range(1, 4)]
        client = MagicMock()
        client.messages.create.side_effect = [
            _mock_response(_multi_classify(*[(i, 70) for i in ids])),
            _mock_response(_editorial_payload()),
        ]
        client.messages.stream.side_effect = [_mock_stream(_deep_payload(*ids))]
        scorer = Scorer(cfg, client=client, min_call_interval=0)
        scorer.analyze([_raw(i) for i in ids])

        sent_max_tokens = client.messages.stream.call_args.kwargs["max_tokens"]
        assert sent_max_tokens == Scorer._deep_max_tokens(3)
        assert sent_max_tokens >= 3 * DEEP_TOKENS_PER_ITEM_ESTIMATE

    def test_output_token_cap_stops_deep_analysis(self, config):
        cfg = _deep_config(config)
        cfg.cost_caps.max_output_tokens_per_run = 100
        client = MagicMock()
        # classification consumes 200 output tokens (> cap); deep must be skipped
        client.messages.create.side_effect = [
            _mock_response(_classify_payload("item-001", 85), output_tokens=200),
            _mock_response(_editorial_payload()),
        ]
        scorer = Scorer(cfg, client=client, min_call_interval=0)
        analyzed, _, _ = scorer.analyze([_raw("item-001")])

        assert analyzed[0].deep_analysis is None      # cap hit → no depth
        assert client.messages.create.call_count == 2 # classify + editorial
        client.messages.stream.assert_not_called()    # no deep call

    def test_deep_failure_leaves_item_without_depth(self, config):
        cfg = _deep_config(config)
        cfg.scoring_rubric.never_discard = []
        client = MagicMock()
        client.messages.create.side_effect = [
            _mock_response(_classify_payload("item-001", 85)),
            _mock_response(_editorial_payload()),
        ]
        # Both deep attempts fail at the stream helper.
        client.messages.stream.side_effect = [Exception("deep fail 1"), Exception("deep fail 2")]
        scorer = Scorer(cfg, client=client, min_call_interval=0)
        analyzed, editorial, _ = scorer.analyze([_raw("item-001")])

        assert len(analyzed) == 1
        assert analyzed[0].deep_analysis is None       # quarantined depth, item survives
        assert client.messages.stream.call_count == 2  # retried once
        assert editorial is not None


def _ids_in(kwargs) -> list[str]:
    """Item ids referenced in a request's user prompt (empty for editorial)."""
    return re.findall(r"ITEM_ID: (\S+)", kwargs["messages"][0]["content"])


class TestConcurrency:
    """max_concurrency > 1 must stay correct: same results, thread-safe cost,
    cap honored per wave. Responses are keyed off the request (not an ordered
    side_effect list) so concurrent consumption can't make these flaky."""

    def _classify_client(self, out_tokens: int = 50) -> MagicMock:
        client = MagicMock()
        def create_se(*a, **k):
            ids = _ids_in(k)
            if ids:
                return _mock_response(_multi_classify(*[(i, 85) for i in ids]), output_tokens=out_tokens)
            return _mock_response(_editorial_payload(), output_tokens=5)
        client.messages.create.side_effect = create_se
        return client

    def test_concurrent_classification_covers_all_items(self, config):
        items = [_raw(f"item-{i:03d}") for i in range(12)]
        scorer = Scorer(config, client=self._classify_client(),
                        batch_size=1, min_call_interval=0, max_concurrency=4)
        analyzed, _, _ = scorer.analyze(items)
        assert {a.item_id for a in analyzed} == {f"item-{i:03d}" for i in range(12)}

    def test_concurrent_matches_serial_result(self, config):
        items = [_raw(f"item-{i:03d}") for i in range(7)]
        serial = Scorer(config, client=self._classify_client(), batch_size=2, min_call_interval=0)
        par = Scorer(config, client=self._classify_client(), batch_size=2,
                     min_call_interval=0, max_concurrency=4)
        s_ids = {a.item_id for a in serial.analyze(items)[0]}
        p_ids = {a.item_id for a in par.analyze(items)[0]}
        assert s_ids == p_ids

    def test_concurrent_cap_stops_within_one_wave(self, config):
        config.cost_caps.max_output_tokens_per_run = 50
        items = [_raw(f"item-{i:03d}") for i in range(20)]
        scorer = Scorer(config, client=self._classify_client(out_tokens=100),
                        batch_size=1, min_call_interval=0, max_concurrency=4)
        analyzed, _, _ = scorer.analyze(items)
        # Each 1-item batch outputs 100 (> 50 cap); the first wave of 4 all run,
        # then the cap halts further waves. So at most one wave's worth survives.
        assert 0 < len(analyzed) <= 4

    def test_deep_batches_do_not_overlap(self, config):
        # Serial (deterministic): each item must appear in exactly one deep batch.
        # Guards the slicing fix (was sliced by classification batch size, causing
        # overlapping deep batches that re-processed items).
        cfg = _deep_config(config)
        cfg.deep_analysis.deep_batch_size = 3
        cfg.scoring_rubric.never_discard = []
        items = [_raw(f"item-{i:03d}") for i in range(10)]
        client = self._classify_client()
        seen: list[list[str]] = []
        def stream_se(*a, **k):
            ids = _ids_in(k)
            seen.append(ids)
            return _mock_stream(_deep_payload(*ids))
        client.messages.stream.side_effect = stream_se
        scorer = Scorer(cfg, client=client, min_call_interval=0)   # serial
        analyzed, _, _ = scorer.analyze(items)

        flat = [i for batch in seen for i in batch]
        assert len(flat) == len(set(flat))                       # no item in two batches
        assert set(flat) == {f"item-{i:03d}" for i in range(10)}  # every item covered once
        assert len(seen) == 4                                     # ceil(10/3) non-overlapping batches
        assert all(a.deep_analysis is not None for a in analyzed)


class TestRunCostPhases:
    def test_phases_tracked_and_reported_separately(self):
        cost = _RunCost()
        u1 = MagicMock(input_tokens=1000, output_tokens=1000)   # classification (cheap)
        u2 = MagicMock(input_tokens=2000, output_tokens=2000)   # deep analysis (cheap)
        cost.add(PHASE_CLASSIFICATION, CHEAP_MODEL, u1)
        cost.add(PHASE_DEEP_ANALYSIS, CHEAP_MODEL, u2)

        # cheap: $1/MTok in, $5/MTok out
        assert cost.phase_usd(PHASE_CLASSIFICATION) == pytest.approx(1000*1e-6 + 1000*5e-6)
        assert cost.phase_usd(PHASE_DEEP_ANALYSIS) == pytest.approx(2000*1e-6 + 2000*5e-6)
        # deep is separable even though it shares the model with classification
        assert cost.phase_usd(PHASE_DEEP_ANALYSIS) > cost.phase_usd(PHASE_CLASSIFICATION)
        assert cost.estimate_usd() == pytest.approx(
            cost.phase_usd(PHASE_CLASSIFICATION) + cost.phase_usd(PHASE_DEEP_ANALYSIS)
        )
        assert cost.phase_usd(PHASE_EDITORIAL) == 0.0   # never added


# ─── Prompt quality (Stage 1: BLUF, specific, calibrated uncertainty) ──────

class TestPromptQuality:
    def test_classification_prompt_demands_bluf_and_uncertainty(self, config):
        p = build_classification_system_prompt(config)
        assert "BLUF" in p
        assert "specific" in p.lower()
        assert "calibrated uncertainty" in p.lower()
        assert "do not invent" in p.lower()

    def test_editorial_prompt_demands_bluf_and_grounding(self, config):
        p = build_editorial_prompt([], config)
        assert "bottom line" in p.lower() or "BLUF" in p
        assert "ground every claim" in p.lower() or "do not invent" in p.lower()
