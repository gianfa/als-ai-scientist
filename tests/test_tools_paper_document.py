"""`tools.paper.document`: the extraction indexed by heading and served in bounded, located passages."""

from __future__ import annotations

import hashlib

import pytest

from tools.paper.document import MAX_PASSAGE_LINES, PaperDocument

MARKDOWN = """# Autoimmune response to C9orf72

<a id="p0001"></a>

Neuroinflammation is apparent in the affected tissues<sup>7,8</sup>.

[Figure on PDF page 2 (1)](../assets/figure/figure-p0002-000.jpg)

**Fig. 1 | Immunophenotyping shows alterations in CD4<sup>+</sup> subsets.**

# **Donor characteristics**

Participants with ALS ( _n_ = 19) and healthy control donors were enrolled.

# **Methods**

## **Statistical analysis**

Two-tailed Mann-Whitney tests were used throughout the study.

# **References**

- 1. Miao, J. _Reimagining research papers_. Nature (2026).

## **Conversion notes**

Converted from PDF.
"""


@pytest.fixture
def document(tmp_path):
    path = tmp_path / "paper.md"
    path.write_text(MARKDOWN, encoding="utf-8")
    return PaperDocument(path)


def test_the_index_names_every_heading_with_its_range(document):
    nav = document.navigate()
    assert nav["sha256"] == hashlib.sha256(document.path.read_bytes()).hexdigest()
    assert nav["max_passage_lines"] == MAX_PASSAGE_LINES
    headings = [s["heading"] for s in nav["sections"]]
    assert headings == [
        "Autoimmune response to C9orf72",
        "Donor characteristics",
        "Methods",
        "Statistical analysis",
        "References",
        "Conversion notes",
    ]
    first, second = nav["sections"][0], nav["sections"][1]
    assert first["lines"][0] == 1 and first["lines"][1] == second["lines"][0] - 1
    assert first["level"] == 1 and nav["sections"][3]["level"] == 2


def test_a_container_heading_is_not_outside_the_main_text_it_is_empty(document):
    """`Methods` only introduces subsections: there is nothing in it to quote, which is not the same thing."""
    methods = next(s for s in document.navigate()["sections"] if s["heading"] == "Methods")
    assert methods["outside_main_text"] is False
    assert document.passage(heading="Methods")["text"] == ""


def test_reference_and_conversion_sections_are_flagged_outside(document):
    flagged = [s["heading"] for s in document.navigate()["sections"] if s["outside_main_text"]]
    assert flagged == ["References", "Conversion notes"]


def test_a_served_passage_is_normalised_and_carries_its_provenance(document):
    served = document.passage(heading="Donor characteristics")
    assert served["status"] == "served"
    assert served["sha256"] == document.sha256
    assert served["heading"] == "Donor characteristics"
    assert served["text"] == "Participants with ALS ( n = 19) and healthy control donors were enrolled."
    assert served["outside_main_text"] is False and served["note"] is None


def test_a_caption_is_flagged_and_told_what_to_do_about_it(document):
    caption_line = (
        MARKDOWN.splitlines().index("**Fig. 1 | Immunophenotyping shows alterations in CD4<sup>+</sup> subsets.**") + 1
    )
    served = document.passage(start=caption_line, end=caption_line)
    assert served["outside_main_text"] is True
    assert "check_quote cannot return 'exact'" in served["note"]
    assert "CD4+ subsets" in served["text"]


def test_a_request_wider_than_the_bound_is_refused_not_truncated(tmp_path):
    path = tmp_path / "long.md"
    path.write_text("# Long\n" + "\n".join(f"line {n}" for n in range(200)), encoding="utf-8")
    document = PaperDocument(path)
    refused = document.passage(start=1, end=1 + MAX_PASSAGE_LINES)
    assert refused["status"] == "refused" and refused["reason"] == "too_wide"
    assert str(MAX_PASSAGE_LINES) in refused["message"]
    assert "text" not in refused


@pytest.mark.parametrize(
    "kwargs, reason",
    [
        ({"start": 0, "end": 3}, "out_of_range"),
        ({"start": 3, "end": 2}, "out_of_range"),
        ({"start": 1, "end": 10_000}, "out_of_range"),
        ({"heading": "No such section"}, "heading_not_found"),
        ({}, "no_target"),
        ({"start": 3}, "no_target"),
    ],
)
def test_a_request_that_cannot_be_served_says_why(document, kwargs, reason):
    refused = document.passage(**kwargs)
    assert refused["status"] == "refused" and refused["reason"] == reason
    assert refused["sha256"] == document.sha256


def test_a_heading_is_found_by_a_unique_prefix_but_never_by_a_guess(document):
    assert document.find("donor char").heading == "Donor characteristics"
    assert document.find("Autoimmune response to C9orf72") is not None
    assert document.find("") is None
