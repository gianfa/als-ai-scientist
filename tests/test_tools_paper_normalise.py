"""`tools.paper.normalise`: the extraction's markup removed without damaging the paper's words."""

from __future__ import annotations

import pytest

from tools.paper.normalise import normalise_passage


@pytest.mark.parametrize(
    "raw, expected",
    [
        # The rule the measurement paid for: a citation marker goes, a sign that belongs to the word stays.
        ("T cell infiltration<sup>7,8</sup> is apparent", "T cell infiltration is apparent"),
        ("CD4<sup>+</sup> T cells release IL-5", "CD4+ T cells release IL-5"),
        ("CD45RA<sup>−</sup>CCR7<sup>+</sup>", "CD45RA−CCR7+"),
        ("T<sub>N</sub> and T<sub>EMRA</sub>", "TN and TEMRA"),
        ("a reference range<sup>12–14</sup> only", "a reference range only"),
        # Anchors, stray tags and escaped entities the conversion left behind.
        ('<a id="p0007"></a>Mutations in C9orf72', "Mutations in C9orf72"),
        ("&lt;i&gt;C9orf72&lt;/i&gt; regulates", "<i>C9orf72</i> regulates"),
        ("ALS &amp; FTD", "ALS & FTD"),
        # Links keep the paper's words, not the conversion's target.
        (
            "available at [https://doi.org/10.1038/x](<https://doi.org/10.1038/x>).",
            "available at https://doi.org/10.1038/x.",
        ),
        # Emphasis goes; an underscore inside a word is the word's.
        ("**Code availability**", "Code availability"),
        ("the _Nature_ paper", "the Nature paper"),
        ("IL_6 and snake_case stay", "IL_6 and snake_case stay"),
        # Whitespace collapses last, so a removed marker leaves no double space.
        ("one<sup>3</sup>  two\n  three", "one two three"),
    ],
)
def test_normalise_passage(raw: str, expected: str) -> None:
    assert normalise_passage(raw) == expected


@pytest.mark.parametrize(
    "raw, expected",
    [
        # Markup the conversion left malformed. A bare `<sup>` is still markup and goes.
        ("the level of CD4<sup> rose sharply", "the level of CD4 rose sharply"),
        ("value <sup>a<sup>2</sup></sup> shown", "value a shown"),
        ("a value<sup></sup> here", "a value here"),
        # An empty script tag may carry the only space between two words: dropping it would glue them.
        ("a value<sup> </sup>here", "a value here"),
        # Ambiguous content is kept rather than guessed at.
        ("CD4<sup>+7</sup> T cells", "CD4+7 T cells"),
        ("P <sup>&lt;</sup> 0.05", "P < 0.05"),
    ],
)
def test_malformed_or_ambiguous_markup(raw: str, expected: str) -> None:
    assert normalise_passage(raw) == expected


def test_a_tag_the_paper_is_talking_about_survives_as_text() -> None:
    """An escaped tag is the paper's own words, not markup: entities resolve after tags are stripped."""
    assert normalise_passage("the converter emits &lt;sup&gt;7&lt;/sup&gt; for a citation") == (
        "the converter emits <sup>7</sup> for a citation"
    )


def test_normalise_passage_is_idempotent() -> None:
    once = normalise_passage("CD4<sup>+</sup> T cells<sup>7,8</sup> in **ALS**")
    assert normalise_passage(once) == once == "CD4+ T cells in ALS"


def test_plain_prose_is_returned_unchanged() -> None:
    prose = "Neuroinflammation is apparent in the affected tissues."
    assert normalise_passage(prose) == prose
