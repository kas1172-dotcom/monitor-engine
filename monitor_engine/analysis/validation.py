from __future__ import annotations

import html
import re

from monitor_engine.models import AnalyzedItem, RawItem

# ─── Title normalization ────────────────────────────────────────────────────
# Some sources (esp. FDA recall feeds) dump a full product/packaging string as
# the "title": comma-separated descriptors, distributor boilerplate, NDC/lot
# codes, SCREAMING CAPS. normalize_title turns those into a readable headline
# while leaving already-clean headlines essentially untouched (just length-capped).

_TITLE_MAX_LEN = 80

# Tail boilerplate to cut: everything from a distributor/packaging/regulatory
# marker onward is packaging metadata, not headline content.
_BOILERPLATE_TAIL_RE = re.compile(
    r"\s*[,;:]?\s*\b(?:distributed by|manufactured by|manufactured for|packaged by|"
    r"marketed by|made in|product of|rx only|ndc\b|udi\b|lot (?:number|#|no))\b.*$",
    re.IGNORECASE,
)
# Trailing source attribution like " | FDA" or " - Federal Register". Requires
# whitespace around the separator so hyphenated words ("Build-to-Print") are safe.
_SOURCE_SUFFIX_RE = re.compile(r"\s+[|–—]\s+[^|–—]{1,40}$|\s+-\s+[^|–—\-]{1,40}$")
_WS_RE = re.compile(r"\s+")
# A word is "screaming" if its letters are all uppercase and longer than a
# typical acronym — so FDA/CMS/USP/HCl survive but CARBONATE/TABLETS get fixed.
_ACRONYM_MAX_LEN = 4


def _descream_word(word: str) -> str:
    core = re.sub(r"[^A-Za-z]", "", word)
    if len(core) > _ACRONYM_MAX_LEN and core.isupper():
        return word.capitalize()   # ULTRA→Ultra, keeps trailing punctuation
    return word


def normalize_title(raw_title: str) -> str:
    """Produce a clean, human-readable headline from a possibly-clunky source title.

    Deterministic and conservative: decodes entities, collapses whitespace, cuts
    distributor/packaging boilerplate tails, de-SCREAMs long all-caps words (short
    acronyms preserved), and caps length at a word boundary. Never returns empty —
    falls back to the trimmed original if normalization would erase everything.
    """
    if not raw_title:
        return raw_title
    text = _WS_RE.sub(" ", html.unescape(raw_title)).strip()
    text = _BOILERPLATE_TAIL_RE.sub("", text).rstrip(" ,;:-")
    text = _SOURCE_SUFFIX_RE.sub("", text).rstrip(" ,;:-") or text
    text = " ".join(_descream_word(w) for w in text.split(" "))
    if len(text) > _TITLE_MAX_LEN:
        text = text[:_TITLE_MAX_LEN].rsplit(" ", 1)[0].rstrip(" ,;:-") + "…"
    return text.strip() or raw_title.strip()

# ─── Shared multiplier table ───────────────────────────────────────────────

_MULTIPLIERS: dict[str, float] = {
    "k": 1_000,
    "thousand": 1_000,
    "m": 1_000_000,
    "million": 1_000_000,
    "b": 1_000_000_000,
    "billion": 1_000_000_000,
    "t": 1_000_000_000_000,
    "trillion": 1_000_000_000_000,
}

_CURRENCY_SYMBOLS: dict[str, str] = {
    "$": "USD",
    "€": "EUR",
    "£": "GBP",
    "¥": "JPY",
    "₹": "INR",
}

# Matches optional currency symbol, a number, an optional multiplier word, and optional ISO code
_DOLLAR_RE = re.compile(
    r"([$€£¥₹])?"
    r"\s*(\d[\d,]*(?:\.\d+)?)\s*"
    r"(trillion|billion|million|thousand|[tbmk])?"
    r"\s*(USD|EUR|GBP|JPY|INR)?",
    re.IGNORECASE,
)

# Matches a number, optional multiplier, optional trailing unit word(s)
_POPULATION_RE = re.compile(
    r"(\d[\d,]*(?:\.\d+)?)\s*"
    r"(trillion|billion|million|thousand|[tbmk])?\s*"
    r"([a-z][a-z ]{0,29})?",
    re.IGNORECASE,
)

_STOP_WORDS = frozenset(["the", "a", "an", "of", "in", "for", "and", "or", "to"])

# Numbers appearing in source text (with optional multiplier suffix)
_SOURCE_NUM_RE = re.compile(
    r"\d[\d,]*(?:\.\d+)?"
    r"(?:\s*(?:trillion|billion|million|thousand|[tbmk]))?",
    re.IGNORECASE,
)


# ─── Dollar amount parser ──────────────────────────────────────────────────

def parse_dollar_amount(raw_text: str) -> tuple[float | None, str | None]:
    """Return (value_in_base_units, currency_iso) or (None, None) on parse failure."""
    if not raw_text:
        return None, None
    m = _DOLLAR_RE.search(raw_text)
    if not m:
        return None, None
    sym, num_str, suffix, iso = m.groups()
    try:
        value = float(num_str.replace(",", ""))
    except ValueError:
        return None, None
    if suffix:
        value *= _MULTIPLIERS.get(suffix.lower(), 1)
    currency: str | None = None
    if iso:
        currency = iso.upper()
    elif sym and sym in _CURRENCY_SYMBOLS:
        currency = _CURRENCY_SYMBOLS[sym]
    return value, currency


# ─── Population parser ─────────────────────────────────────────────────────

def parse_affected_population(raw_text: str) -> tuple[int | None, str | None]:
    """Return (value_as_int, unit_string) or (None, None) on parse failure."""
    if not raw_text:
        return None, None
    m = _POPULATION_RE.search(raw_text)
    if not m:
        return None, None
    num_str, suffix, unit_str = m.groups()
    try:
        value = float(num_str.replace(",", ""))
    except ValueError:
        return None, None
    if suffix:
        value *= _MULTIPLIERS.get(suffix.lower(), 1)
    int_value = int(value)
    if int_value <= 0:
        return None, None
    unit: str | None = None
    if unit_str:
        cleaned = unit_str.strip().lower()
        if cleaned and cleaned not in _STOP_WORDS:
            unit = cleaned
    return int_value, unit


# ─── Factual grounding ─────────────────────────────────────────────────────

# A number with an optional multiplier suffix that may be glued ("40m") or
# spaced ("40 million"). Used to canonicalise source-text numbers the same way
# parse_dollar_amount / parse_affected_population canonicalise the extracted value.
_NUM_SUFFIX_RE = re.compile(
    r"^(\d[\d,]*(?:\.\d+)?)\s*(trillion|billion|million|thousand|[tbmk])?$",
    re.IGNORECASE,
)


def _parse_source_number(token: str) -> float | None:
    """Parse a number token to canonical base units, handling both glued
    ('40m', '$40,000,000') and spaced ('40 million') multiplier forms."""
    m = _NUM_SUFFIX_RE.match(token.strip())
    if not m:
        return None
    num_str, suffix = m.groups()
    try:
        base = float(num_str.replace(",", ""))
    except ValueError:
        return None
    if suffix:
        base *= _MULTIPLIERS.get(suffix.lower(), 1)
    return base


def _value_in_source(value: float, source_text: str, tolerance: float = 0.20) -> bool:
    """Return True if `value` or a close variant (within tolerance) appears in source_text."""
    if value <= 0:
        return True  # can't verify zeros meaningfully
    for tok in _SOURCE_NUM_RE.findall(source_text):
        candidate = _parse_source_number(tok)
        if candidate is not None and candidate > 0:
            if abs(candidate - value) / max(abs(value), 1) <= tolerance:
                return True
    return False


def validate_factual_claims(item: AnalyzedItem, raw: RawItem) -> AnalyzedItem:
    """
    Verify dollar amounts, populations, and deadlines against the source title+summary.
    Unverifiable claims are added to unverified_claims and confidence_note is flagged.
    """
    source_text = f"{raw.title} {raw.summary or ''}".lower()
    unverified: list[str] = []

    if item.dollar_amount and item.dollar_amount.value is not None:
        if not _value_in_source(item.dollar_amount.value, source_text):
            unverified.append(f"dollar_amount {item.dollar_amount.raw_text!r} not in source")

    if item.affected_population and item.affected_population.value is not None:
        if not _value_in_source(item.affected_population.value, source_text):
            unverified.append(
                f"affected_population {item.affected_population.raw_text!r} not in source"
            )

    if item.action_deadline:
        dl = item.action_deadline
        date_patterns = [
            dl.isoformat(),
            f"{dl.month}/{dl.day}/{dl.year}",
            f"{dl.month:02d}/{dl.day:02d}/{dl.year}",
            str(dl.year),
        ]
        if not any(p in source_text for p in date_patterns):
            unverified.append(f"action_deadline {dl.isoformat()!r} not in source")

    if not unverified:
        return item

    flag = "UNVERIFIED: " + "; ".join(unverified)
    existing = item.confidence_note or ""
    new_note = f"{flag}. {existing}".rstrip(". ").rstrip() if existing else flag

    return item.model_copy(update={
        "confidence_note": new_note,
        "unverified_claims": list(unverified),
    })
