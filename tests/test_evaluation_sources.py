"""The per-run register of sources read.

It is derivative by design: every field is copied from an artifact the run already persisted. So the
tests assert the join — that four disagreeing identifier forms meet as one source — and the reporting of
what the join cannot settle, which is where the useful findings turn out to be.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evaluation.sources import RunArtifactsMissing, build, main, normalise
from knowledge_base.schemas import canonical_hash

MICHAELIS = "doi:10.1038/s41586-025-09588-6"


def _claim(cid: str, content: str, refs: list[str], run_id: str = "r1") -> dict:
    return {
        "id": cid,
        "run_id": run_id,
        "created_at": "2026-10-06T00:00:00Z",
        "canonical_hash": canonical_hash(content),
        "type": "observational",
        "content": content,
        "origin": "literature",
        "provenance_status": "verified",
        "verification_method": "quote",
        "source_refs": refs,
        "entity_refs": [],
        "quote": "a passage",
        "cause_ref": None,
        "effect_ref": None,
        "sign": None,
        "context": None,
        "mechanism": None,
    }


@pytest.fixture
def archive(tmp_path: Path) -> tuple[str, Path, Path, Path]:
    """One run that read three sources by four different spellings, plus one it could not fetch."""
    results = tmp_path / "exp" / "r1" / "results"
    (results / "workflows").mkdir(parents=True)

    (results / "literature-calls.jsonl").write_text(
        "\n".join(
            json.dumps(c)
            for c in (
                {"paper": MICHAELIS, "verdict": "exact", "receipt_id": "rec-1"},
                {"paper": MICHAELIS, "verdict": "near", "receipt_id": "rec-2"},
                {"paper": "pmid:29542041", "verdict": "absent", "receipt_id": "rec-3"},
                {"paper": "doi:10.1016/j.celrep.2022.110838", "verdict": "unverifiable"},
            )
        )
        + "\n"
    )
    (results / "05-review-report.json").write_text(
        json.dumps({"fetched": {"10.1016/j.celrep.2022.110838": "failed", "10.3389/fimmu.2015.00651": "fetched"}})
    )
    (results / "workflows" / "03-literature.mcp.json").write_text(
        json.dumps({"mcpServers": {"paper": {"args": ["-m", "paper_tools.mcp_server", "--paper", "/x/paper.md"]}}})
    )

    kb = tmp_path / "kb"
    kb.mkdir()
    (kb / "claims.jsonl").write_text(
        "\n".join(
            json.dumps(c)
            for c in (
                # the same paper, cited as a bare DOI rather than as `doi:…`
                _claim("C-1", "a claim on the main paper", ["10.1038/s41586-025-09588-6", "receipt:abc"]),
                _claim("C-2", "a claim citing a PMID", ["PMID:29542041"]),
                _claim("C-3", "a claim citing a source nobody can look up", ["Web search results"]),
                _claim("C-4", "a claim of another run", ["PMID:9999999"], run_id="r2"),
            )
        )
        + "\n"
    )

    store = tmp_path / "store"
    store.mkdir()
    (store / "index.jsonl").write_text(
        "\n".join(
            json.dumps(r)
            for r in (
                {
                    "canonical_id": MICHAELIS,
                    "aliases": ["pmid:41034581", "pmcid:PMC12657233"],
                    "title": "Autoimmune response to C9orf72 protein",
                    "year": "2025",
                    "licence": "cc by-nc-nd",
                    "source_url": "https://example.org/a",
                    "status": "full_text",
                    "sha256": "aaa",
                    "access_date": "2026-09-30T21:32:46Z",
                    "run_id": "r0",
                },
                {
                    "canonical_id": "pmid:29542041",
                    "title": "An older paper",
                    "year": "2018",
                    "licence": "cc by",
                    "status": "not_available",
                    "sha256": None,
                    "access_date": "2026-10-01T00:00:00Z",
                },
                {
                    "canonical_id": "doi:10.3389/fimmu.2015.00651",
                    "title": "A fetched paper",
                    "year": "2015",
                    "licence": "cc by",
                    "status": "full_text",
                    "sha256": "bbb",
                },
            )
        )
        + "\n"
    )
    return "r1", results, kb, store / "index.jsonl"


def test_four_spellings_of_one_source_become_one_entry(archive) -> None:
    """A claim writes a bare DOI, the quote log writes `doi:…`, the store knows a PMID and a PMCID."""
    register = build(*archive)
    michaelis = [s for s in register["sources"] if s["id"] == MICHAELIS]
    assert len(michaelis) == 1
    assert michaelis[0]["cited_as"] == ["10.1038/s41586-025-09588-6", MICHAELIS]
    assert michaelis[0]["quote_checks"] == {"exact": 1, "near": 1}
    assert michaelis[0]["cited_by_claims"] == ["C-1"]


def test_catalogue_metadata_is_copied_not_invented(archive) -> None:
    source = next(s for s in build(*archive)["sources"] if s["id"] == MICHAELIS)
    assert source["title"] == "Autoimmune response to C9orf72 protein"
    assert source["licence"] == "cc by-nc-nd"
    assert source["text_sha256"] == "aaa"
    assert source["first_seen_run"] == "r0"


def test_a_source_the_store_never_saw_is_named(archive) -> None:
    """The run cited it, the fetch failed, the quote could not be checked: the reader must see all three."""
    register = build(*archive)
    assert register["absent_from_store"] == ["doi:10.1016/j.celrep.2022.110838"]
    missing = next(s for s in register["sources"] if s["id"] == "doi:10.1016/j.celrep.2022.110838")
    assert missing["in_store"] is False
    assert missing["fetched_during_run"] == "failed"
    assert missing["quote_checks"] == {"unverifiable": 1}
    assert missing["title"] is None  # absent, never guessed


def test_a_source_held_without_full_text_is_distinguished_from_one_never_seen(archive) -> None:
    register = build(*archive)
    assert register["in_store_without_full_text"] == ["pmid:29542041"]
    assert "pmid:29542041" not in register["absent_from_store"]


def test_a_reference_that_names_no_identifier_is_reported_not_dropped(archive) -> None:
    """A claim whose source is the string "Web search results" names no identifier."""
    assert build(*archive)["unresolved_identifiers"] == {"Web search results": 1}


def test_only_this_run_s_claims_count_as_citations(archive) -> None:
    register = build(*archive)
    assert all(not s["id"].endswith("9999999") for s in register["sources"])


def test_a_fetched_paper_appears_even_when_no_claim_cites_it(archive) -> None:
    fetched = next(s for s in build(*archive)["sources"] if s["id"] == "doi:10.3389/fimmu.2015.00651")
    assert fetched["fetched_during_run"] == "fetched"
    assert fetched["cited_by_claims"] == []


def test_the_counts_agree_with_the_rows(archive) -> None:
    register = build(*archive)
    counts, sources = register["counts"], register["sources"]
    assert counts["sources"] == len(sources)
    assert counts["cited_in_claims"] == sum(1 for s in sources if s["cited_by_claims"])
    assert counts["quote_checked"] == sum(1 for s in sources if s["quote_checks"])
    assert counts["licences"] == {"cc by-nc-nd": 1, "cc by": 2}


def test_the_paper_tool_target_is_recorded(archive) -> None:
    """Which extracted text the `paper` server served is part of what the run read."""
    assert build(*archive)["paper_tool_served"] == "/x/paper.md"


def test_the_register_says_what_it_is_derived_from_and_what_it_does_not_assert(archive) -> None:
    register = build(*archive)
    assert "literature-calls.jsonl" in register["derived_from"]
    assert "says nothing about whether the claim resting on it is sound" in register["note"]


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("10.1038/s41586-025-09588-6", "doi:10.1038/s41586-025-09588-6"),
        ("doi:10.1038/S41586-025-09588-6", "doi:10.1038/s41586-025-09588-6"),
        ("PMID:29542041", "pmid:29542041"),
        ("pmid: 29542041", "pmid:29542041"),
        ("PMC11247723", "pmcid:PMC11247723"),
        ("Web search results", None),
        ("", None),
    ],
)
def test_identifier_normalisation(raw: str, expected: str | None) -> None:
    assert normalise(raw) == expected


def test_a_bare_number_is_read_as_a_pmid_by_the_project_s_one_canonicaliser(archive) -> None:
    """A bare `41034581` is the main paper's PMID, and `tools.literature.store.canonical_id` reads any
    bare number that way.

    That is a guess, and it is recorded here as one: a bare number in a `source_refs` list could be
    anything. The alternative — resolving only numbers the store already knows — was written and then
    dropped, because it would have meant a *second* canonicaliser disagreeing with the one the host and
    the ingest path use, which is the fault this change exists to remove. One rule, and its limit stated,
    beats two rules that are each right somewhere.
    """
    run_id, results, kb, index = archive
    claims = (kb / "claims.jsonl").read_text()
    claims += json.dumps(_claim("C-9", "a claim citing a bare number", ["41034581"])) + "\n"
    (kb / "claims.jsonl").write_text(claims)

    register = build(run_id, results, kb, index)
    michaelis = next(s for s in register["sources"] if s["id"] == MICHAELIS)
    assert "C-9" in michaelis["cited_by_claims"]  # the store's alias joined it to the main paper


def test_a_claim_identifier_cited_as_a_source_is_reported(archive) -> None:
    """Production runs really do this: a claim whose `source_refs` names another claim."""
    run_id, results, kb, index = archive
    claims = (kb / "claims.jsonl").read_text()
    claims += json.dumps(_claim("C-11", "a claim citing a claim", ["C-76"])) + "\n"
    (kb / "claims.jsonl").write_text(claims)
    assert build(run_id, results, kb, index)["unresolved_identifiers"]["C-76"] == 1


def test_a_run_without_a_results_directory_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(RunArtifactsMissing):
        build("r1", tmp_path / "nowhere", tmp_path, tmp_path / "index.jsonl")


def test_cli_writes_the_register_beside_the_run(archive, tmp_path: Path) -> None:
    _, results, kb, index = archive
    assert main(["r1", "--experiments", str(tmp_path / "exp"), "--kb", str(kb), "--store", str(index.parent)]) == 0
    written = json.loads((results / "sources-read.json").read_text())
    assert written["run_id"] == "r1"
    assert written["generated_by"] == "evaluation/sources.py"


def test_cli_reports_a_run_it_cannot_read(tmp_path: Path, capsys) -> None:
    assert main(["ghost", "--experiments", str(tmp_path), "--kb", str(tmp_path), "--store", str(tmp_path)]) == 1
    assert "skipped" in capsys.readouterr().out
