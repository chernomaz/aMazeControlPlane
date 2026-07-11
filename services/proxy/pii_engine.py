"""
PII redaction engine — Presidio analyzer with two backends selected by env
flag `PII_NLP_MODE`:

  * `regex` (default) — pure regex recognizers, no spaCy model loaded. Small
    image, ~10-20 ms per redact() on a 2 KB payload, misses bare city names
    and single-name references.
  * `ner`             — Presidio `AnalyzerEngine` backed by spaCy
    `en_core_web_md`. ~150 MB extra RSS, ~500 ms extra startup, ~+15-25 ms
    per redact() call, but reliably catches "Nashville", "Paris", single-
    token proper nouns, and disambiguates PERSON vs LOCATION via context.

Both backends expose the same `redact(text, entities)` and
`redact_json_text_fields(obj, entities)` API. The addon and the orchestrator
preview endpoint don't care which backend is active.

**Env vars are read once at import** (#15). Changing `PII_NLP_MODE` or
`PII_SPACY_MODEL` at runtime does NOT take effect until the proxy is
restarted — supervisord manages that in production. This is intentional:
switching modes mid-flight would mean partial requests see one backend
while later ones see another.

Why not `presidio-anonymizer`: its cryptography dep (>=46) conflicts with
mitmproxy's cryptography<44.1. We only need the `replace` operator anyway, so
text splicing is done inline in `_replace_spans()`.

## Batching optimization

`redact_json_text_fields` used to call `redact()` once per string leaf. On a
typical MCP tool response with 30+ string fields that meant 30+ analyzer
passes, dominating latency. It now joins every leaf into one buffer with a
bounded separator (`_LEAF_SEP`), runs a single analyzer pass, and splits the
result back into leaves. The separator is chosen so no built-in recognizer
matches it AND so entity spans cannot cross a boundary.

## PERSON / LOCATION notes

- In `regex` mode, PERSON and LOCATION use lightweight custom regex patterns
  (multi-word capitalized runs for PERSON; numeric-prefix street addresses
  for LOCATION). Both miss bare-word cases.
- In `ner` mode, Presidio's built-in `SpacyRecognizer` handles PERSON /
  LOCATION via the spaCy NER pipeline. Our custom PERSON / LOCATION regex
  recognizers are dropped in that mode to avoid double-tagging.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass

from presidio_analyzer import EntityRecognizer, Pattern, PatternRecognizer
from presidio_analyzer.predefined_recognizers import (
    CreditCardRecognizer,
    EmailRecognizer,
    IbanRecognizer,
    IpRecognizer,
    PhoneRecognizer,
    UrlRecognizer,
)

logger = logging.getLogger(__name__)


PII_NLP_MODE = os.getenv("PII_NLP_MODE", "regex").lower()
PII_SPACY_MODEL = os.getenv("PII_SPACY_MODEL", "en_core_web_lg")

# ---------------------------------------------------------------------------
# Custom regex recognizers (used in BOTH modes; NER mode adds spaCy on top).
# ---------------------------------------------------------------------------

# PERSON: two or three capitalized words in a row. Regex-only fallback.
_PERSON_PATTERN = Pattern(
    name="person_two_or_three_caps",
    regex=r"\b[A-Z][a-z]{1,20}(?:\s+[A-Z][a-z]{1,20}){1,2}\b",
    score=0.4,
)

# LOCATION: a US street address with a common suffix and numeric prefix.
_LOCATION_PATTERN = Pattern(
    name="location_us_street",
    regex=(
        r"\b\d{1,6}\s+[A-Z][A-Za-z0-9\s.'-]{1,60}"
        r"\b(?:Street|St|Avenue|Ave|Boulevard|Blvd|Road|Rd|Lane|Ln|Drive|Dr|"
        r"Way|Court|Ct|Place|Pl|Parkway|Pkwy|Terrace|Ter|Circle|Cir)\b"
        r"(?:,?\s+[A-Z][A-Za-z\s]{1,40})?"
        r"(?:,?\s+[A-Z]{2}(?:\s+\d{5})?)?"
    ),
    score=0.4,
)

# US_SSN: Presidio's built-in wants context; our regex fires standalone.
_SSN_PATTERN = Pattern(
    name="us_ssn_dashes_or_bare",
    regex=r"\b(?!000|666|9\d{2})\d{3}[- ]?(?!00)\d{2}[- ]?(?!0000)\d{4}\b",
    score=0.85,
)


def _person_recognizer() -> PatternRecognizer:
    return PatternRecognizer(
        supported_entity="PERSON",
        patterns=[_PERSON_PATTERN],
        context=["mr", "mrs", "ms", "dr", "prof", "hi", "hello", "dear"],
        global_regex_flags=0,
    )


def _location_recognizer() -> PatternRecognizer:
    return PatternRecognizer(
        supported_entity="LOCATION",
        patterns=[_LOCATION_PATTERN],
        context=["address", "office", "at", "in", "from"],
        global_regex_flags=0,
    )


def _ssn_recognizer() -> PatternRecognizer:
    return PatternRecognizer(
        supported_entity="US_SSN",
        patterns=[_SSN_PATTERN],
    )


# ---------------------------------------------------------------------------
# Regex-only recognizer registry — used by both modes for the entities the
# built-in Presidio recognizers cover well without NER.
# ---------------------------------------------------------------------------

_REGEX_RECOGNIZERS: dict[str, EntityRecognizer] = {
    "EMAIL_ADDRESS": EmailRecognizer(),
    "CREDIT_CARD":   CreditCardRecognizer(),
    "PHONE_NUMBER":  PhoneRecognizer(),
    "US_SSN":        _ssn_recognizer(),
    "IP_ADDRESS":    IpRecognizer(),
    "URL":           UrlRecognizer(),
    "IBAN_CODE":     IbanRecognizer(),
    "PERSON":        _person_recognizer(),
    "LOCATION":      _location_recognizer(),
}


@dataclass(frozen=True)
class _Span:
    start: int
    end: int
    label: str


# ---------------------------------------------------------------------------
# NER backend (Presidio AnalyzerEngine + spaCy). Loaded lazily on first use
# so import doesn't block on the spaCy model when PII_NLP_MODE=regex.
# ---------------------------------------------------------------------------

_NER_ANALYZER = None  # set by _get_ner_analyzer() on first use

# Presidio's SpacyRecognizer surfaces these entities via spaCy NER — we let
# it own PERSON and LOCATION in NER mode; the regex fallbacks for those are
# not registered when the NER engine is active.
_NER_HANDLED_BY_SPACY: frozenset[str] = frozenset({"PERSON", "LOCATION"})


def _get_ner_analyzer():
    """Build (once) an AnalyzerEngine wired to spaCy `en_core_web_lg`
    (overridable via `PII_SPACY_MODEL`).

    Recognizer stack in NER mode:
      * All Presidio built-ins (Email/CreditCard/Phone/Ip/Url/Iban/SpacyRecognizer).
        SpacyRecognizer surfaces PERSON and LOCATION via spaCy's NER.
      * Our custom US_SSN pattern — Presidio's built-in wants surrounding
        context we can't reliably supply.

    We deliberately do NOT register the regex PERSON / LOCATION recognizers
    here. `en_core_web_lg` reliably catches the cases they were compensating
    for (bare city names, uncommon multi-word names like "Uma Lee"), and the
    regex fallback introduced false positives on multi-word capitalized
    phrases that aren't people ("Machine Learning", "United Airlines"). If a
    site trips the accuracy ceiling, plug a Piiranha TransformersRecognizer
    in here rather than reinstating the regex.
    """
    global _NER_ANALYZER
    if _NER_ANALYZER is not None:
        return _NER_ANALYZER

    from presidio_analyzer import AnalyzerEngine
    from presidio_analyzer.nlp_engine import NlpEngineProvider

    logger.info("pii_engine: loading spaCy model %s (NER mode)", PII_SPACY_MODEL)
    provider = NlpEngineProvider(nlp_configuration={
        "nlp_engine_name": "spacy",
        "models": [{"lang_code": "en", "model_name": PII_SPACY_MODEL}],
    })
    nlp_engine = provider.create_engine()

    _NER_ANALYZER = AnalyzerEngine(nlp_engine=nlp_engine, supported_languages=["en"])
    # Fix #11: remove Presidio's built-in UsSsnRecognizer (which needs
    # surrounding-word context we can't reliably supply) before adding our
    # custom one. Both would otherwise fire and duplicate work — _merge_spans
    # would hide the collision but we'd still pay twice.
    try:
        _NER_ANALYZER.registry.remove_recognizer("UsSsnRecognizer")
    except Exception as exc:  # noqa: BLE001 — Presidio raises different types across versions
        logger.debug("pii_engine: could not remove built-in UsSsnRecognizer: %s", exc)
    _NER_ANALYZER.registry.add_recognizer(_ssn_recognizer())
    logger.info("pii_engine: NER analyzer ready")
    return _NER_ANALYZER


def preload_ner_analyzer() -> None:
    """Fix #10: eagerly instantiate the NER analyzer at addon load. Without
    this the first request in NER mode pays ~3-5 seconds of blocking spaCy
    initialization on the event-loop thread, stalling every other flow in
    flight. Safe to call in regex mode too — it becomes a cheap noop."""
    if PII_NLP_MODE != "ner":
        return
    try:
        _get_ner_analyzer()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "pii_engine: NER preload failed, will fall back to regex per request: %s",
            exc,
        )


# ---------------------------------------------------------------------------
# Single-text analysis (dispatch on PII_NLP_MODE)
# ---------------------------------------------------------------------------

def _analyze_regex(text: str, entities: list[str]) -> list[_Span]:
    """Run each requested regex recognizer directly. No spaCy."""
    spans: list[_Span] = []
    for entity in entities:
        rec = _REGEX_RECOGNIZERS.get(entity)
        if rec is None:
            logger.warning("pii_engine: unknown entity %r ignored", entity)
            continue
        try:
            results = rec.analyze(text=text, entities=[entity], nlp_artifacts=None)
        except Exception as exc:  # noqa: BLE001
            logger.warning("pii_engine: recognizer %s raised: %s", entity, exc)
            continue
        for r in results or []:
            spans.append(_Span(start=r.start, end=r.end, label=r.entity_type))
    return _merge_spans(spans)


def _analyze_ner(text: str, entities: list[str]) -> list[_Span]:
    """Single AnalyzerEngine pass — spaCy runs once for all NER-derived
    entities and every recognizer runs against the shared nlp_artifacts.
    """
    try:
        analyzer = _get_ner_analyzer()
    except Exception as exc:  # noqa: BLE001 — model missing → fall back
        logger.warning("pii_engine: NER unavailable, falling back to regex: %s", exc)
        return _analyze_regex(text, entities)
    try:
        results = analyzer.analyze(text=text, entities=list(entities), language="en")
    except Exception as exc:  # noqa: BLE001
        logger.warning("pii_engine: NER analyzer raised, falling back to regex: %s", exc)
        return _analyze_regex(text, entities)
    return _merge_spans([_Span(start=r.start, end=r.end, label=r.entity_type)
                         for r in (results or [])])


def _analyze(text: str, entities: list[str]) -> list[_Span]:
    if PII_NLP_MODE == "ner":
        return _analyze_ner(text, entities)
    return _analyze_regex(text, entities)


def _merge_spans(spans: list[_Span]) -> list[_Span]:
    """Sort by start, drop later spans that overlap earlier ones (prefer first
    match; ties broken by longest span)."""
    if not spans:
        return []
    ordered = sorted(spans, key=lambda s: (s.start, -(s.end - s.start)))
    kept: list[_Span] = []
    for s in ordered:
        if kept and s.start < kept[-1].end:
            continue
        kept.append(s)
    return kept


def _replace_spans(text: str, spans: list[_Span]) -> str:
    """Splice `<LABEL>` markers into `text` at each span. Assumes spans are
    non-overlapping and sorted by start."""
    if not spans:
        return text
    out: list[str] = []
    cursor = 0
    for s in spans:
        out.append(text[cursor:s.start])
        out.append(f"<{s.label}>")
        cursor = s.end
    out.append(text[cursor:])
    return "".join(out)


def redact(text: str, entities: list[str]) -> str:
    """Return `text` with every match of every requested entity replaced by
    `<ENTITY_LABEL>`. Empty input, no matches, or empty entities → returns
    input verbatim. Never raises — analyzer errors fall back to the regex
    path or, ultimately, to the input unchanged.
    """
    if not text or not entities:
        return text
    spans = _analyze(text, entities)
    return _replace_spans(text, spans)


# ---------------------------------------------------------------------------
# JSON-aware per-leaf walk
#
# We used to batch every leaf into one analyzer call using a `_LEAF_SEP`
# marker, but spaCy's NER attributes entity spans across the separator and
# misses names in the batch. Per-leaf analyzer calls give consistent accuracy
# (spaCy sees each value in isolation) at the cost of one analyzer call per
# string. The regex path pays the same cost either way; the NER path gets
# ~5-10 percentage-point better recall in exchange for the slower walk.
#
# `_try_parse_json_container` is the JSON-in-string trick: when a leaf is
# itself a serialized JSON container (like the `text` field of an MCP tool
# result), we parse it into structure and recurse. The analyzer then sees
# bare values ("Grace Hall") rather than JSON-escaped noise
# (`"{\"name\":\"Grace Hall\"}"`).
# ---------------------------------------------------------------------------


# Fix #9: cap `json.loads` on incidentally-JSON-shaped strings. A 10 MB SQL
# response field that happens to start with `{` shouldn't cost a full parse
# on every redact call. 64 KB covers realistic MCP tool payloads (rows,
# search results) while bounding the worst case.
_MAX_JSON_PARSE_SIZE = 64 * 1024

# Fix #13: cap recursion. A hostile or misbehaving MCP server could return
# 10_000-deep nested lists and stack-overflow the proxy. Truncate any subtree
# past this depth to a placeholder marker.
_MAX_JSON_DEPTH = 32
_DEPTH_PLACEHOLDER = "<PII_STRUCTURE_TOO_DEEP>"


def _try_parse_json_container(s: str) -> object | None:
    """If `s` parses as a JSON dict or list, return the parsed object.
    Everything else (plain strings, JSON numbers/bools/null/quoted strings) →
    None. The cheap "starts with {[" gate avoids paying `json.loads` on the
    99% of leaves that are just prose; the size cap avoids the worst case.
    """
    if not s or s[0] not in "{[":
        return None
    if len(s) > _MAX_JSON_PARSE_SIZE:
        return None
    stripped = s.lstrip()
    if not stripped or stripped[0] not in "{[":
        return None
    try:
        parsed = json.loads(s)
    except (ValueError, TypeError):
        return None
    if isinstance(parsed, (dict, list)):
        return parsed
    return None


def safe_redact_json(obj: object, entities: list[str]) -> object:
    """Shape-preserving wrapper around `redact_json_text_fields`. If the walk
    raises for any reason, returns the input **unchanged** — the shape the
    caller expected is preserved (dict stays dict, list stays list, string
    stays string). Log the failure so we notice, but do NOT invent a
    different type in the return value.

    Rationale: two callers (audit_log and pii_redactor) previously each had
    their own local helper with **opposite** failure semantics (audit
    returned input, redactor returned a bare string that broke shape). One
    canonical helper prevents that drift.
    """
    try:
        return redact_json_text_fields(obj, entities)
    except Exception as exc:  # noqa: BLE001 — wrap all analyzer/parse errors
        logger.warning("pii_engine: safe_redact_json failed, returning input as-is: %s", exc)
        return obj


def redact_json_text_fields(obj: object, entities: list[str], _depth: int = 0) -> object:
    """Recursively redact every string leaf inside a JSON-like structure.

    JSON-in-string leaves (e.g. the `text` field of an MCP tool result when
    the tool returns pure JSON) are parsed and walked, so the analyzer sees
    bare leaf values rather than the encoded JSON wrapper. On the way back
    out, the parsed structure is re-serialized (compact separators, #8) so
    wire format stays close to the caller's input size.

    Non-string leaves (numbers, booleans, null) pass through untouched.

    `_depth` is internal (#13). Any subtree past `_MAX_JSON_DEPTH` is
    replaced with `<PII_STRUCTURE_TOO_DEEP>` — bounds stack size against a
    hostile or misbehaving MCP server that returns deeply nested data.
    """
    if not entities:
        return obj
    if _depth >= _MAX_JSON_DEPTH:
        return _DEPTH_PLACEHOLDER
    if isinstance(obj, str):
        parsed = _try_parse_json_container(obj)
        if parsed is not None:
            return json.dumps(
                redact_json_text_fields(parsed, entities, _depth + 1),
                separators=(",", ":"),
                ensure_ascii=False,
            )
        return redact(obj, entities)
    if isinstance(obj, list):
        return [redact_json_text_fields(x, entities, _depth + 1) for x in obj]
    if isinstance(obj, dict):
        return {k: redact_json_text_fields(v, entities, _depth + 1) for k, v in obj.items()}
    return obj
