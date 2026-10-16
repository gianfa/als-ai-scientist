"""The audit packet for one proposal (`evaluation/citation_audit.py`): deterministic, over files, no judgement."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from kb_factories import NOW, claim, experiment, hypothesis, ignorance, relation

from evaluation import citation_audit

LATER = NOW + timedelta(days=1)

QUOTE = "IL-10 responses are greater in donors with a longer predicted survival time"
PROPOSAL = {
    "run_id": "RUN-1",
    "proposal": {
        "outcome": "proposal",
        "mechanism": {
            "text": "The axis rests on a verified finding [[C-1]]. A second claim has a quote whose paper is "
            "absent [[C-2]]. A third quotes nothing at all [[C-3]]. The reasoning step is ours [[C-4]]. "
            "The experiment that would settle it is [[EXP-1]], and the gap is [[I-1]].",
            "cites": ["C-1", "C-2", "C-3", "C-4", "EXP-1", "I-1"],
        },
    },
    "gate": {
        "passed": True,
        "cites_contradicted_or_contested": {"mechanism": ["C-4 (contradicted)"]},
        "unknown_ids": {},
    },
}


class FakeStore:
    """The literature store is not what is under test here; `check_quote` is injected through the module."""


def kb(tmp_path: Path) -> Path:
    """The records written straight to the files `read_records` reads: this module only reads a knowledge base."""
    from knowledge_base.store import FILES

    root = tmp_path / "kb"
    root.mkdir(parents=True, exist_ok=True)
    quoted = claim(id="C-1", content="A verified finding.", quote=QUOTE)
    absent = claim(
        id="C-2",
        content="A claim whose paper is not in the store.",
        quote=QUOTE,
        source_refs=["PMC9999999"],
        provenance_status="unverified",
        verification_method="none",
    )
    unsourced = claim(
        id="C-3",
        content="A quote with no source.",
        quote=QUOTE,
        origin="agent",
        provenance_status="unverified",
        verification_method="none",
        source_refs=[],
    )
    reasoning = claim(
        id="C-4",
        content="Our own reasoning step.",
        origin="agent",
        provenance_status="unverified",
        verification_method="none",
        source_refs=[],
    )
    # A later run's evidence: this is what makes "as of the end of the run" differ from "today".
    contra = claim(id="C-5", content="Evidence against the reasoning step.", run_id="RUN-2", created_at=LATER)
    records = [
        quoted,
        absent,
        unsourced,
        reasoning,
        contra,
        hypothesis(id="H-1"),
        ignorance(id="I-1", hypothesis_id="H-1"),
        experiment(
            id="EXP-1",
            hypothesis_id="H-1",
            tested_claims=["C-1"],
            expected_outcomes=[
                {"outcome": "a", "claim_id": "C-1", "effect": "supports"},
                {"outcome": "b", "claim_id": "C-1", "effect": "contradicts"},
            ],
        ),
        relation(
            id="L-1",
            type="questions",
            source_id="I-1",
            target_id="C-1",
            justification_method="agent_inference",
            provenance_status="unverified",
            source_refs=[],
        ),
        relation(id="L-2", type="contradicts", source_id="C-5", target_id="C-4", run_id="RUN-2", created_at=LATER),
    ]
    for record in records:
        path = root / FILES[type(record)]
        with path.open("a", encoding="utf-8") as handle:
            handle.write(record.model_dump_json() + "\n")
    return root


def archive(tmp_path: Path) -> Path:
    results = tmp_path / "results"
    results.mkdir(parents=True, exist_ok=True)
    (results / "final-proposal.json").write_text(json.dumps(PROPOSAL))
    return results


def fake_check(monkeypatch):
    """`check_quote` answers from the claim's source ref: one paper is in the store, one is not."""

    def check(store, log, paper, quote):
        if paper == "doi:10.1038/x":
            return {"status": "checked", "verdict": "exact", "text_sha256": "a" * 64, "cause": None}
        return {"status": "unverifiable", "verdict": None, "text_sha256": None, "cause": "text_not_available"}

    import tools.literature.mcp_server as server

    monkeypatch.setattr(server, "check_quote", check)
    import tools.literature.store as store_module

    monkeypatch.setattr(store_module, "LiteratureStore", lambda root: FakeStore())


def audit(tmp_path, monkeypatch):
    fake_check(monkeypatch)
    return citation_audit.rows(archive(tmp_path), kb(tmp_path), tmp_path / "store", tmp_path / "log.jsonl")


def test_every_citation_lands_on_a_rung_of_the_support_ladder(tmp_path, monkeypatch):
    data = audit(tmp_path, monkeypatch)

    rungs = {row["id"]: row["rung"] for row in data["rows"]}
    assert rungs == {
        "C-1": "quote_checked",
        "C-2": "source_text_absent",
        "C-3": "no_source_ref",
        "C-4": "no_quote",
        "EXP-1": "derived",
        "I-1": "derived",
    }
    assert citation_audit.ladder(data)["by_rung"] == {
        "quote_checked": 1,
        "source_text_absent": 1,
        "no_source_ref": 1,
        "no_quote": 1,
        "derived": 2,
    }
    assert citation_audit.ladder(data)["unit"] == "cites tags"


def test_a_checked_quote_records_the_hash_of_the_text_it_read(tmp_path, monkeypatch):
    """The store is mutable and git-ignored, so an audit that does not pin what it read cannot be re-run."""
    data = audit(tmp_path, monkeypatch)

    row = next(r for r in data["rows"] if r["id"] == "C-1")
    assert row["text_sha256"] == "a" * 64 and row["check"] == "checked"
    absent = next(r for r in data["rows"] if r["id"] == "C-2")
    assert absent["check"] == "unverifiable" and absent["check_cause"] == "text_not_available"


def test_a_citation_carries_the_sentence_that_names_it(tmp_path, monkeypatch):
    """`cites` is per section; the inline tags are what recover the sentence a reader judges."""
    data = audit(tmp_path, monkeypatch)

    row = next(r for r in data["rows"] if r["id"] == "C-3")
    assert row["sentences"] and "quotes nothing at all" in row["sentences"][0]["sentence"]
    assert row["sentences"][0]["at"] == "mechanism.text"


def test_a_derived_record_is_traced_to_what_it_rests_on(tmp_path, monkeypatch):
    data = audit(tmp_path, monkeypatch)

    assert next(r for r in data["rows"] if r["id"] == "EXP-1")["rests_on"] == ["C-1"]
    assert next(r for r in data["rows"] if r["id"] == "I-1")["rests_on"] == ["C-1"]


def test_the_status_at_the_end_of_the_run_is_reported_beside_the_status_today(tmp_path, monkeypatch):
    """A later relation contradicted C-4 after the run ended; both values are shown, neither is silently used."""
    data = audit(tmp_path, monkeypatch)

    row = next(r for r in data["rows"] if r["id"] == "C-4")
    assert (row["status_at_run_end"], row["status_now"]) == ("unresolved", "contradicted")


def test_the_known_weaknesses_follow_a_rule_fixed_before_counting(tmp_path, monkeypatch):
    data = audit(tmp_path, monkeypatch)
    found = citation_audit.weaknesses(data)

    assert found["rule"] == ["quote cannot be checked", "flagged by the gate", "status moved since the run"]
    kinds = {w["id"]: w["kind"] for w in found["weaknesses"]}
    assert kinds == {
        "C-2": "quote cannot be checked",
        "C-3": "quote cannot be checked",
        "C-4": "flagged by the gate",
    }  # C-4 also moved status; the gate flag is reported


def test_the_packet_asks_for_a_verdict_only_where_a_human_can_give_one(tmp_path, monkeypatch):
    text = citation_audit.packet(audit(tmp_path, monkeypatch))

    assert "| C-1 | quote_checked |" in text and "| C-3 | no_source_ref |" in text
    assert "| C-4 |" not in text.split("## Known weaknesses")[0].split("## To read")[1]  # no quote, nothing to read
    assert "cited to limit" in text and "Known weaknesses" in text


def test_a_missing_proposal_is_a_reason_not_an_empty_audit(tmp_path):
    data = citation_audit.rows(tmp_path, tmp_path, tmp_path, tmp_path / "log.jsonl")

    assert data["rows"] is None and "no final-proposal.json" in data["reason"]


def test_the_filled_packet_is_read_back_with_limiting_citations_out_of_the_denominator(tmp_path):
    filled = tmp_path / "citations.md"
    filled.write_text(
        "| id | rung | claim | quote | sentence | source | verdict | note |\n"
        "|---|---|---|---|---|---|---|---|\n"
        "| C-1 | quote_checked | x | y | s | z | supports |  |\n"
        "| C-2 | source_text_absent | x | y | s | z | overstates |  |\n"
        "| C-3 | no_source_ref | x | y | s | z | cited to limit |  |\n"
        "| C-4 | source_text_absent | x | y | s | z | cannot tell by reading |  |\n"
    )

    out = citation_audit.read_verdicts(filled)

    assert out["read"] == 4 and out["counts"]["supports"] == 1
    assert out["denominator"] == 2 and out["accuracy"] == 0.5  # limiting and undecidable rows are excluded
    assert out["undecidable_by_reading"] == 1


def test_the_weakness_table_is_read_back_as_the_inverse_test(tmp_path):
    """Not what the proposal declares, but which of the weaknesses the artifacts know about it leaves out."""
    filled = tmp_path / "citations.md"
    filled.write_text(
        "| id | rung | claim | quote | sentence | source | verdict | note |\n"
        "|---|---|---|---|---|---|---|---|\n"
        "| C-1 | quote_checked | x | y | s | z | supports |  |\n"
        "\n## Known weaknesses: does the proposal state each one?\n\n"
        "| id | kind | detail | stated | note |\n"
        "|---|---|---|---|---|\n"
        "| C-2 | quote cannot be checked | text_not_available | stated |  |\n"
        "| C-3 | quote cannot be checked | no_source_ref | not stated |  |\n"
        "| C-4 | flagged by the gate | status contested |  |  |\n"
    )

    out = citation_audit.read_verdicts(filled)

    assert out["weaknesses_read"] == 2  # the unmarked row is not counted as either
    assert out["weaknesses_stated"] == 1 and out["weaknesses_not_stated"] == ["C-3"]
    assert out["read"] == 1 and out["accuracy"] == 1.0  # the two tables are read apart
