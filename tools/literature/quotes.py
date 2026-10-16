"""Check a quote against the stored text of a paper, on tokens, with three verdicts.

Tokens are words, numbers (with decimals) and the symbols `< > = + % ± ≥ ≤`; punctuation is ignored, because
text extracted from JATS/PDF carries orphan punctuation ("spinal cord,. Autoimmune") that a copied quote
cannot reproduce. Verdicts:

- `exact`      the quote's tokens occur as one contiguous run in the source;
- `near`       not contiguous, but at least `near_min` of the quote's content tokens sit in one window of
               1.5 x the quote length (an elision, a dropped word): NOT an acceptance; `passage` is that
               stretch (padded) for an agent to look at, and whatever the agent answers is checked again;
- `absent`     too little overlap, fewer than `MIN_QUOTE_TOKENS` tokens, or an empty input.

`critical_mismatch` is true when a number, symbol or negation of the quote is missing from the window: such a
quote is never `exact`, and the reader must be told. `score` is the share of the quote's content tokens (stop
words excluded, with multiplicity) found in the best window. Known limits: hyphenated line breaks from PDF
text are not repaired, and text an extractor dropped (tables, formulas) cannot be found.

Pure functions, no I/O.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from typing import Final

MIN_QUOTE_TOKENS: Final = 5
NEAR_MIN_SCORE: Final = (
    0.7  # see the science note: between 80 other-paper quotes (max 0.60) and 19 non-contiguous ones (min 0.77)
)

_STOP_TOKENS: Final = frozenset(
    {
        "the",
        "a",
        "an",
        "of",
        "in",
        "and",
        "to",
        "is",
        "are",
        "was",
        "were",
        "that",
        "with",
        "for",
        "by",
        "on",
        "as",
        "at",
        "from",
        "or",
        "be",
        "this",
        "these",
        "those",
        "it",
        "its",
        "we",
        "here",
    }
)
_NEGATIONS: Final = frozenset({"not", "no", "non", "without", "neither", "nor", "never", "none", "lack", "lacked"})
_SYMBOLS: Final = "<>=+%±≥≤"
# ASCII digits only and letters/digits without underscore: the same token set as the JavaScript reference.
_TOKEN: Final = re.compile(rf"[0-9]+(?:[.,][0-9]+)*|[^\W_]+|[{re.escape(_SYMBOLS)}]")


@dataclass(frozen=True)
class QuoteCheck:
    verdict: str  # exact | near | absent
    score: float
    critical_mismatch: bool
    passage: str | None  # exact: the matched stretch; near: the padded stretch; absent: None
    reason: str


@dataclass(frozen=True)
class _Token:
    text: str
    start: int
    end: int


def _tokenize(text: str) -> list[_Token]:
    return [_Token(m.group(0), m.start(), m.end()) for m in _TOKEN.finditer(text)]


def _normalise(text: str) -> str:
    return unicodedata.normalize("NFKC", text).lower()


def _is_critical(token: str) -> bool:
    return token[0].isdigit() or token in _NEGATIONS or (len(token) == 1 and token in _SYMBOLS)


def _absent(reason: str) -> QuoteCheck:
    return QuoteCheck("absent", 0.0, False, None, reason)


def classify_quote(quote: str, source_text: str, *, near_min: float = NEAR_MIN_SCORE) -> QuoteCheck:
    """Classify `quote` against `source_text`; never raises on bad input."""
    if not isinstance(quote, str) or not quote.strip():
        return _absent("quote is empty or not a string")
    if not isinstance(source_text, str) or not source_text:
        return _absent("source text is empty or not a string")
    q = [t.text for t in _tokenize(_normalise(quote))]
    if len(q) < MIN_QUOTE_TOKENS:
        return _absent(f"quote has fewer than {MIN_QUOTE_TOKENS} tokens, too short to identify a passage")
    # Offsets refer to the normalised source (NFKC can change lengths); the passage is cut from that text.
    normalised = unicodedata.normalize("NFKC", source_text)
    src = _tokenize(normalised.lower())
    if not src:
        return _absent("source text has no tokens")
    toks = [t.text for t in src]
    n = len(q)
    for i in range(len(toks) - n + 1):
        if toks[i : i + n] == q:
            return QuoteCheck(
                "exact",
                1.0,
                False,
                normalised[src[i].start : src[i + n - 1].end],
                "quote found contiguously in the source text",
            )
    content = [t for t in q if t not in _STOP_TOKENS]
    if not content:
        return _absent("quote has no content tokens")
    need: dict[str, int] = {}
    for token in content:
        need[token] = need.get(token, 0) + 1
    width = min(len(toks), math.ceil(n * 1.5))
    have: dict[str, int] = {}
    hit = 0
    best, best_start = -1, 0

    def add(token: str, sign: int) -> None:
        nonlocal hit
        wanted = need.get(token)
        if wanted is None:
            return
        before = have.get(token, 0)
        after = before + sign
        have[token] = after
        if sign > 0 and before < wanted:
            hit += 1
        if sign < 0 and after < wanted:
            hit -= 1

    for i, token in enumerate(toks):
        add(token, 1)
        if i >= width:
            add(toks[i - width], -1)
        if i >= width - 1 and hit > best:
            best, best_start = hit, i - width + 1
    score = max(best, 0) / len(content)
    window = set(toks[best_start : best_start + width])
    critical = any(_is_critical(t) and t not in window for t in q)
    if score < near_min:
        return QuoteCheck("absent", score, critical, None, "too little overlap with any stretch of the source")
    # Padded on both sides: the best window can stop just short of the end of an elided quote.
    pad = max(5, math.ceil(n * 0.25))
    start = max(best_start - pad, 0)
    end = min(best_start + width + pad, len(src)) - 1
    return QuoteCheck(
        "near",
        score,
        critical,
        normalised[src[start].start : src[end].end],
        "not contiguous, but most content tokens sit in one stretch of the source",
    )


# What a non-exact verdict means, given where else the quote (or something close to it) sits and how complete the text is.
FOUND: Final = "found"  # exact in the main text
OUTSIDE_MAIN: Final = "in_paper_outside_main_text"  # exact in tables, captions, formulas or supplements
NOT_LITERAL: Final = "not_literal"  # close to a passage, not as written: paraphrase, elision, joined fragments
NOT_IN_TEXT: Final = "not_in_stored_text"  # nowhere in a text that covers the whole paper: hallucination or paraphrase
MAYBE_INCOMPLETE: Final = (
    "not_found_text_may_be_incomplete"  # nowhere, but the stored text is known to miss parts of the paper
)


@dataclass(frozen=True)
class QuoteAssessment:
    check: QuoteCheck  # the verdict on the main text (what `verified` depends on)
    located_in: str | None  # main | extras | references | None: where an exact match was found
    near_in: str | None  # where the closest passage is, when there is no exact match
    assessment: str  # one of the constants above
    text_completeness: str  # full_jats | partial | unknown


def assess_quote(
    quote: str, main: str, extras: str = "", refs: str = "", *, completeness: str = "unknown"
) -> QuoteAssessment:
    """`classify_quote` on the main text, then a judgment on a non-exact result: is the quote missing from the paper, or from
    the stored text? `completeness` says how much of the paper the stored text covers (`full_jats`: main + extras + references
    are all there, so `absent` means not in the paper; `partial` or `unknown`: parts may be missing, so it does not)."""
    check = classify_quote(quote, main)
    if check.verdict == "exact":
        return QuoteAssessment(check, "main", None, FOUND, completeness)
    for name, text in (("extras", extras), ("references", refs)):
        if not text:
            continue
        other = classify_quote(quote, text)
        if other.verdict == "exact":
            return QuoteAssessment(check, name, None, OUTSIDE_MAIN, completeness)
    if check.verdict == "near":
        return QuoteAssessment(check, None, "main", NOT_LITERAL, completeness)
    for name, text in (("extras", extras), ("references", refs)):
        if text and classify_quote(quote, text).verdict == "near":
            return QuoteAssessment(check, None, name, NOT_LITERAL, completeness)
    return QuoteAssessment(
        check, None, None, NOT_IN_TEXT if completeness == "full_jats" else MAYBE_INCOMPLETE, completeness
    )
