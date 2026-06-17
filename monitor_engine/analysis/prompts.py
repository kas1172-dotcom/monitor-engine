from __future__ import annotations

from monitor_engine.models import AnalyzedItem, ClientConfig, RawItem


def build_classification_system_prompt(config: ClientConfig) -> str:
    rubric = config.scoring_rubric
    t = rubric.thresholds

    editions_block = []
    for edition in config.editions:
        cats = ", ".join(edition.categories) if edition.categories else "(none)"
        editions_block.append(
            f"EDITION ID: {edition.id}\n"
            f"  Label: {edition.label}\n"
            f"  Audience: {edition.audience_description}\n"
            f"  Instructions: {edition.analysis_instructions}\n"
            f"  Valid categories: {cats}"
        )

    never_discard_note = ""
    if rubric.never_discard:
        kws = ", ".join(f'"{k}"' for k in rubric.never_discard)
        never_discard_note = (
            f"\nItems whose title or summary contain any of these keywords must always be "
            f"analyzed (even if relevance is below {t.tier_3_min}): {kws}."
        )

    return f"""You are an intelligence analyst writing briefing copy for busy professionals. \
You will receive a batch of news/intelligence items. For each item, score it for every \
edition listed below and extract structured facts.

EDITIONS TO ANALYZE:
{chr(10).join(editions_block)}

WRITING STYLE (applies to so_what and now_what):
- BLUF — bottom line up front: lead with the consequence, not background.
- Be specific, not generic. Name the agency, program, rule, company, or figure from the
  source. Avoid filler like "this could have significant implications" or "stakeholders
  should monitor developments" — say what implication, for whom.
- Calibrated uncertainty: state only what the source supports. If something is proposed vs.
  final, partial, or unclear, say so plainly. Never manufacture specifics or false precision.

SCORING RULES:
- relevance_score: integer 0–100 measuring how relevant the item is to the edition's audience.
  80–100: directly actionable; 50–79: useful context; 20–49: marginal; 0–19: not relevant.
- so_what: 1–2 sentences, BLUF, on the concrete consequence for this edition's audience.
- now_what: one concrete, specific action this audience should take or consider.
- categories: only values from the edition's "Valid categories" list; empty list if none apply.
{never_discard_note}
EXTRACTION RULES:
- dollar_amount_raw: copy the exact monetary text if present (e.g. "$1.2 billion", "€500 million"). \
Null if none.
- affected_population_raw: copy the exact count text if present \
(e.g. "50,000 soldiers", "12 aircraft", "3 bases"). Null if none.
- action_deadline: nearest specific upcoming deadline as YYYY-MM-DD. Null if none.
- confidence_note: brief note when an assessment is uncertain or the source is thin/ambiguous. \
Null only when genuinely confident.

Do NOT invent facts, figures, or deadlines not present in the source text; an absent detail is \
"not stated", never a guess. Analyze ALL items in the batch.
""".strip()


def _format_items_block(items: list[RawItem], *, summary_chars: int = 600) -> str:
    """Render a batch of items as ITEM_ID-keyed blocks. Shared by the
    classification and deep-analysis user prompts."""
    parts: list[str] = []
    for item in items:
        parts.append(f"ITEM_ID: {item.id}")
        parts.append(f"Title: {item.title}")
        if item.summary:
            parts.append(f"Summary: {item.summary[:summary_chars]}")
        parts.append(f"Source: {item.source_name}")
        if item.published_date:
            parts.append(f"Published: {item.published_date.date()}")
        parts.append("")
    return "\n".join(parts).strip()


def build_classification_user_prompt(items: list[RawItem]) -> str:
    return (
        f"Analyze the following {len(items)} item(s):\n\n"
        + _format_items_block(items)
    )


def build_deep_analysis_system_prompt(config: ClientConfig) -> str:
    da = config.deep_analysis
    if da is None:
        raise ValueError("build_deep_analysis_system_prompt called without deep_analysis config")

    section_lines = []
    for s in da.sections:
        shape = "a list of short strings" if s.kind == "list" else "a single string"
        section_lines.append(f'- "{s.id}" ({s.label}; {shape}): {s.instruction}')

    return f"""You are a senior analyst producing in-depth briefings on individual items.
{da.instruction}

For each item, return a "sections" object containing exactly these keys:
{chr(10).join(section_lines)}

Ground every statement in the item's own content; do not invent facts not \
supported by the source. Produce in-depth analysis for ALL items in the batch.
""".strip()


def build_deep_analysis_user_prompt(items: list[RawItem]) -> str:
    return (
        f"Produce in-depth analysis for the following {len(items)} item(s):\n\n"
        + _format_items_block(items)
    )


def build_editorial_prompt(items: list[AnalyzedItem], config: ClientConfig) -> str:
    edition_labels = {e.id: e.label for e in config.editions}

    top_items = sorted(
        [it for it in items if it.tier in (1, 2)],
        key=lambda x: x.importance_score,
        reverse=True,
    )[:20]

    item_lines = []
    for it in top_items:
        best = max(it.per_edition.items(), key=lambda kv: kv[1].relevance_score, default=None)
        if best:
            eid, analysis = best
            label = edition_labels.get(eid, eid)
            item_lines.append(f"- [Tier {it.tier}] {it.title}")
            item_lines.append(f"  ({label}): {analysis.so_what}")

    items_text = "\n".join(item_lines) if item_lines else "(no top items this run)"

    return f"""You are the editor of {config.branding.name}. \
Based on this run's top items, write a brief editorial package.

TOP ITEMS:
{items_text}

Write exactly three fields:
- theme_of_week: one sentence naming the dominant concrete theme across this run's most
  important items (name the specific thread, not "various developments").
- editors_note: 2–3 sentences of editorial commentary on the specific pattern, risk, or
  opportunity — BLUF, grounded in the items above, not platitudes.
- whats_new_digest: 3–5 sentences on what is genuinely new or changed this run.

Be concise, authoritative, and specific; lead with the bottom line. Ground every claim in the
items above — do not invent developments or overstate certainty. Do not repeat item titles verbatim.
""".strip()
