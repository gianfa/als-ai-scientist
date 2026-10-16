"""Tests for tools.literature.quotes; the cases in the shared fixture are also run by the JS test."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.literature import quotes

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "classify_quote_cases.json").read_text(encoding="utf-8"))


def _cases() -> list[tuple[str, str, str, dict]]:
    shared = [(c["name"], c["quote"], FIXTURE["source"], c) for c in FIXTURE["cases"]]
    own = [(c["name"], c["quote"], c["source"], c) for c in FIXTURE["source_cases"]]
    return shared + own


@pytest.mark.parametrize("name, quote, source, case", _cases(), ids=[c[0] for c in _cases()])
def test_shared_cases(name: str, quote: str, source: str, case: dict) -> None:
    result = quotes.classify_quote(quote, source)
    if "verdict" in case:
        assert result.verdict == case["verdict"], result
    if "verdict_not" in case:
        assert result.verdict != case["verdict_not"], result
    if "critical_mismatch" in case:
        assert result.critical_mismatch is case["critical_mismatch"], result
    if "passage_contains" in case:
        assert result.passage is not None and case["passage_contains"] in result.passage


def test_exact_has_score_one_and_absent_has_no_passage() -> None:
    exact = quotes.classify_quote("ALS PATHOLOGY and it is hypothesized that T cells", FIXTURE["source"])
    assert exact.score == 1.0 and exact.passage
    absent = quotes.classify_quote(
        "Treatment with the drug did not alter the course of the disease in any patient group", FIXTURE["source"]
    )
    assert absent.passage is None and absent.score < quotes.NEAR_MIN_SCORE


def test_non_string_inputs_are_absent_and_never_raise() -> None:
    for bad in (None, 42, b"bytes"):
        assert quotes.classify_quote(bad, FIXTURE["source"]).verdict == "absent"  # type: ignore[arg-type]
        assert quotes.classify_quote("Autoimmune responses are thought to have a key role", bad).verdict == "absent"  # type: ignore[arg-type]


def test_near_min_option_moves_the_near_absent_boundary() -> None:
    q = "Autoimmune responses are thought to lead to rapid loss of neurons in patients"
    assert quotes.classify_quote(q, FIXTURE["source"], near_min=0.99).verdict == "absent"
    assert quotes.classify_quote(q, FIXTURE["source"], near_min=0.3).verdict == "near"


MAIN = FIXTURE["source"]
TABLE = "Table 1 Serum IL-5 concentration was 12.4 pg per millilitre in patients carrying the repeat expansion"
ABSENT_Q = "Treatment with the drug did not alter the course of the disease in any patient group"


def test_assess_found_in_main() -> None:
    a = quotes.assess_quote(
        "Autoimmune responses are thought to have a key role in ALS pathology", MAIN, completeness="full_jats"
    )
    assert a.assessment == quotes.FOUND and a.located_in == "main"


def test_assess_quote_only_in_extras_is_real_but_outside_main() -> None:
    q = "Serum IL-5 concentration was 12.4 pg per millilitre in patients carrying the repeat expansion"
    a = quotes.assess_quote(q, MAIN, TABLE, completeness="full_jats")
    assert a.check.verdict == "absent" and a.assessment == quotes.OUTSIDE_MAIN and a.located_in == "extras"


def test_assess_near_is_not_literal_and_near_in_extras_too() -> None:
    a = quotes.assess_quote(
        "Autoimmune responses are thought ... contribute to the rapid loss of neurons", MAIN, completeness="full_jats"
    )
    assert a.assessment == quotes.NOT_LITERAL and a.near_in == "main"
    b = quotes.assess_quote(
        "Serum IL-5 concentration was 12.4 pg per millilitre in patients with the expansion", MAIN, TABLE
    )
    assert b.assessment == quotes.NOT_LITERAL and b.near_in == "extras"


def test_assess_absent_depends_on_how_complete_the_text_is() -> None:
    assert quotes.assess_quote(ABSENT_Q, MAIN, TABLE, "refs", completeness="full_jats").assessment == quotes.NOT_IN_TEXT
    for completeness in ("partial", "unknown"):
        assert quotes.assess_quote(ABSENT_Q, MAIN, completeness=completeness).assessment == quotes.MAYBE_INCOMPLETE
