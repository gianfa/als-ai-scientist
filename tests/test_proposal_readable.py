"""The readable proposal renderer must add nothing a model wrote and hide nothing the archive holds."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evaluation.proposal_readable import Flags, ProposalMissing, describe, main, render
from knowledge_base.schemas import canonical_hash
from knowledge_base.store import read_records

CLAIM = {
    "id": "C-1",
    "run_id": "r",
    "created_at": "2026-10-06T00:00:00Z",
    "canonical_hash": canonical_hash("IL-10 responses associate with longer survival"),
    "type": "observational",
    "content": "IL-10 responses associate with longer survival",
    "origin": "literature",
    "provenance_status": "verified",
    "verification_method": "quote",
    "source_refs": ["PMID:123", "receipt:abc"],
    "entity_refs": [],
    "quote": "IL-10-mediated responses were higher in individuals with longer survival",
    "cause_ref": None,
    "effect_ref": None,
    "sign": None,
    "context": None,
    "mechanism": None,
}
# An `agent` claim: the schema lets this kind name no source, and 304 of the archive's claims are of it.
# It is exactly the record a reader cannot look up, so the renderer has to say so out loud.
BARE = {
    **CLAIM,
    "id": "C-2",
    "content": "A claim with nothing behind it",
    "canonical_hash": canonical_hash("A claim with nothing behind it"),
    "quote": None,
    "source_refs": [],
    "origin": "agent",
    "verification_method": "none",
    "provenance_status": "unverified",
}
COMPUTED = {
    **CLAIM,
    "id": "C-3",
    "content": "a4: rho = -0.194; n = 21",
    "canonical_hash": canonical_hash("a4: rho = -0.194; n = 21"),
    "quote": None,
    "origin": "computation",
    "verification_method": "code",
    "source_refs": ["result:a4@abc"],
}
REL = {
    "id": "L-1",
    "run_id": "r",
    "created_at": "2026-10-06T00:00:00Z",
    "source_id": "C-1",
    "target_id": "C-2",
    "type": "contradicts",
    "justification_method": "quote",
    "provenance_status": "verified",
    "source_refs": ["receipt:rel1"],
    "rationale": "r",
}


def _kb(tmp_path: Path, records=(CLAIM, BARE, COMPUTED, REL)) -> Path:
    root = tmp_path / "kb"
    root.mkdir()
    buckets = {"C": "claims.jsonl", "L": "relations.jsonl"}
    for rec in records:
        name = buckets[rec["id"].split("-")[0]]
        with (root / name).open("a") as fh:
            fh.write(json.dumps(rec) + "\n")
    return root


def _archive(tmp_path: Path, proposal: dict, gate: dict | None = None) -> Path:
    results = tmp_path / "run" / "results"
    results.mkdir(parents=True)
    (results / "final-proposal.json").write_text(json.dumps({"proposal": proposal, "gate": gate or {"passed": True}}))
    return results


def test_prose_is_preserved_verbatim_apart_from_the_tags(tmp_path: Path) -> None:
    text = render(
        "r",
        _kb(tmp_path),
        _archive(
            tmp_path,
            {"outcome": "proposal", "mechanism": {"text": "The mechanism rests on [[C-1]] alone.", "cites": ["C-1"]}},
        ),
    )
    assert "The mechanism rests on [1] alone." in text
    assert "[[C-1]]" not in text


def test_a_cited_claim_is_described_from_its_own_fields(tmp_path: Path) -> None:
    records = read_records(_kb(tmp_path))
    lines = describe("C-1", records, {"C-1": "supported"}, Flags({}, {}, {}))
    body = "\n".join(lines)
    assert "IL-10 responses associate with longer survival." in body
    assert "supported" in body
    assert "PMID:123" in body
    assert "verification receipt" in body


def test_a_claim_with_no_quote_and_no_source_says_so(tmp_path: Path) -> None:
    records = read_records(_kb(tmp_path))
    body = "\n".join(describe("C-2", records, {"C-2": "contradicted"}, Flags({}, {}, {})))
    assert "No quote is recorded" in body
    assert "No source is named" in body


def test_a_computed_claim_is_not_reported_as_missing_a_quote(tmp_path: Path) -> None:
    records = read_records(_kb(tmp_path))
    body = "\n".join(describe("C-3", records, {"C-3": "unresolved"}, Flags({}, {}, {})))
    assert "No quote is recorded" not in body
    assert "verified by code" in body


def test_a_verified_relation_becomes_a_sentence(tmp_path: Path) -> None:
    records = read_records(_kb(tmp_path))
    body = "\n".join(describe("C-2", records, {"C-2": "contradicted"}, Flags({}, {}, {})))
    assert body.count("Given C-1") == 1
    assert "contradicted" in body


def test_a_cited_id_the_archive_does_not_hold_is_named_as_missing(tmp_path: Path) -> None:
    records = read_records(_kb(tmp_path))
    body = "\n".join(describe("C-99", records, {}, Flags({}, {}, {})))
    assert "Not present in this knowledge base" in body


def test_gate_warnings_reach_the_reader(tmp_path: Path) -> None:
    gate = {
        "passed": True,
        "cites_contradicted_or_contested": {"mechanism": ["C-2 (contradicted)"]},
        "cites_judged_unfaithful": {"mechanism": ["C-2 (judged unfaithful: L-1 (confirmed))"]},
    }
    text = render(
        "r",
        _kb(tmp_path),
        _archive(tmp_path, {"outcome": "proposal", "mechanism": {"text": "Rests on [[C-2]].", "cites": ["C-2"]}}, gate),
    )
    assert "⚠" in text
    assert "say more than its quote supports" in text


def test_each_record_is_numbered_once_however_often_it_is_cited(tmp_path: Path) -> None:
    text = render(
        "r",
        _kb(tmp_path),
        _archive(
            tmp_path,
            {
                "outcome": "proposal",
                "mechanism": {"text": "[[C-1]] and again [[C-1]].", "cites": ["C-1"]},
                "biomarker": {"text": "Also [[C-1]], plus [[C-2]].", "cites": ["C-1", "C-2"]},
            },
        ),
    )
    assert "[1] and again [1]." in text
    assert "Also [1], plus [2]." in text
    assert text.count("1. **C-1**") == 1


def test_failure_modes_are_rendered_and_their_citations_resolved(tmp_path: Path) -> None:
    text = render(
        "r",
        _kb(tmp_path),
        _archive(
            tmp_path,
            {"outcome": "proposal", "failure_modes": [{"text": "It may fail because of [[C-2]].", "cites": ["C-2"]}]},
        ),
    )
    assert "## H. Failure modes" in text
    assert "1. It may fail because of [1]." in text


def test_a_proposal_citing_nothing_is_reported_as_citing_nothing(tmp_path: Path) -> None:
    text = render(
        "r",
        _kb(tmp_path),
        _archive(
            tmp_path, {"outcome": "insufficient_evidence", "mechanism": {"text": "No citations here.", "cites": []}}
        ),
    )
    assert "cites no record" in text


def test_a_run_without_a_proposal_is_an_error_not_an_empty_document(tmp_path: Path) -> None:
    (tmp_path / "run" / "results").mkdir(parents=True)
    with pytest.raises(ProposalMissing):
        render("r", _kb(tmp_path), tmp_path / "run" / "results")


def test_cli_skips_a_run_without_a_proposal_and_reports_it(tmp_path: Path, capsys) -> None:
    (tmp_path / "experiments" / "r" / "results").mkdir(parents=True)
    code = main(["r", "--kb", str(_kb(tmp_path)), "--experiments", str(tmp_path / "experiments")])
    assert code == 1
    assert "skipped" in capsys.readouterr().out


def test_cli_writes_one_file_per_run(tmp_path: Path) -> None:
    exp = tmp_path / "experiments"
    results = exp / "r1" / "results"
    results.mkdir(parents=True)
    (results / "final-proposal.json").write_text(
        json.dumps(
            {
                "proposal": {"outcome": "proposal", "mechanism": {"text": "On [[C-1]].", "cites": ["C-1"]}},
                "gate": {"passed": True},
            }
        )
    )
    assert main(["r1", "--kb", str(_kb(tmp_path)), "--experiments", str(exp)]) == 0
    assert (exp / "final-proposals" / "r1.md").exists()


def test_the_document_states_that_no_model_wrote_it(tmp_path: Path) -> None:
    """The whole point of the renderer: a reader must know the annotations are deterministic."""
    text = render(
        "r",
        _kb(tmp_path),
        _archive(tmp_path, {"outcome": "proposal", "mechanism": {"text": "On [[C-1]].", "cites": ["C-1"]}}),
    )
    assert "No model was" in text


# --- the hypothesis graph -----------------------------------------------------------------------------------


def _graph_kb(tmp_path: Path, with_hypothesis: bool = True) -> Path:
    from kb_factories import CAUSAL, claim, entity, hypothesis, ignorance, relation

    from knowledge_base.store import KnowledgeBase

    root = tmp_path / "gkb"
    records = [
        entity(id="N-1", canonical_name="C9orf72 expansion", run_id="r"),
        entity(id="N-2", canonical_name="motor-neuron loss", run_id="r"),
        claim(id="C-1", type="causal", content="The expansion drives motor-neuron loss.", run_id="r", **CAUSAL),
        ignorance(id="I-1", run_id="r"),
    ]
    if with_hypothesis:
        records += [
            hypothesis(id="H-1", run_id="r"),
            relation(
                id="L-1",
                type="part_of",
                source_id="C-1",
                target_id="H-1",
                run_id="r",
                justification_method="agent_inference",
                provenance_status="unverified",
                source_refs=[],
            ),
        ]
    with KnowledgeBase.open(root) as store:
        store.commit(records, run_id="r")
    return root


def test_the_graph_draws_a_hypothesis_with_its_claims_and_leaves_out_entities_and_gaps(tmp_path: Path) -> None:
    from evaluation.proposal_readable import hypothesis_graph

    text, dropped = hypothesis_graph(read_records(_graph_kb(tmp_path)), "r")
    assert "H_1{{" in text  # hypothesis is a hexagon
    assert "C_1[" in text and "-.->|part_of| H_1" in text  # its claim, linked by an unverified relation
    assert "N_1" not in text and "I_1" not in text  # entities and declared gaps stay out
    assert dropped == 0


def test_the_graph_is_omitted_for_a_run_without_hypotheses(tmp_path: Path) -> None:
    from evaluation.proposal_readable import hypothesis_graph

    assert hypothesis_graph(read_records(_graph_kb(tmp_path, with_hypothesis=False)), "r") is None


def test_the_document_carries_the_graph_legend_and_caveat(tmp_path: Path) -> None:
    from evaluation.proposal_readable import GRAPH_CAVEAT, GRAPH_ID_NOTE, GRAPH_LEGEND

    text = render(
        "r",
        _graph_kb(tmp_path),
        _archive(tmp_path, {"outcome": "proposal", "mechanism": {"text": "On [[C-1]].", "cites": ["C-1"]}}),
    )
    assert "## Hypothesis graph" in text
    assert "```mermaid" in text and GRAPH_CAVEAT in text and GRAPH_ID_NOTE in text
    assert all(item in text for item in GRAPH_LEGEND)
    assert text.index("## Hypothesis graph") < text.index("## Evidence cited")


def test_every_kind_the_graph_can_draw_is_named_in_the_legend() -> None:
    """The legend first shipped explaining two of the four shapes it draws, so a reader met a stadium and a
    rhombus with no key. Asserting that the legend mentions the text of the legend is not enough: this ties it
    to the kinds the graph actually keeps, so a kind added to `GRAPH_KINDS` without a legend entry fails here.
    """
    from evaluation.proposal_readable import GRAPH_KINDS, GRAPH_LEGEND

    legend = " ".join(GRAPH_LEGEND).lower()
    assert sorted(k for k in GRAPH_KINDS if k not in legend) == []


def test_the_document_has_no_graph_section_without_a_hypothesis(tmp_path: Path) -> None:
    text = render(
        "r",
        _kb(tmp_path),
        _archive(tmp_path, {"outcome": "proposal", "mechanism": {"text": "On [[C-1]].", "cites": ["C-1"]}}),
    )
    assert "## Hypothesis graph" not in text


# --- causal chains --------------------------------------------------------------------------------------------


def _causal_kb():
    """Entities N-1..N-5; H-1 of run `r` with a verified step, an unchecked step, a contradicted step and a stray edge."""
    from kb_factories import CAUSAL, claim, entity, hypothesis, relation

    unchecked = {"origin": "agent", "provenance_status": "unverified", "verification_method": "none", "source_refs": []}

    def edge(cid, cause, effect, **kw):
        return claim(
            id=cid,
            type="causal",
            content=f"{cause} causes {effect}",
            **{**CAUSAL, "cause_ref": cause, "effect_ref": effect, **kw},
        )

    records = [
        *(entity(id=f"N-{i}", canonical_name=f"name {i}") for i in range(1, 6)),
        hypothesis(id="H-1", run_id="r"),
        edge("C-10", "N-1", "N-2"),  # verified (factory default)
        edge("C-11", "N-2", "N-3", **unchecked),
        edge("C-12", "N-3", "N-4"),  # verified, then contradicted below
        edge("C-13", "N-5", "N-4", **unchecked),  # not on the chain
        claim(id="C-90", content="Evidence against C-12."),
        relation(id="L-1", type="contradicts", source_id="C-90", target_id="C-12"),
        *(
            relation(
                id=f"L-{10 + n}",
                type="part_of",
                source_id=c,
                target_id="H-1",
                justification_method="agent_inference",
                provenance_status="unverified",
                source_refs=[],
            )
            for n, c in enumerate(["C-10", "C-11", "C-12", "C-13"])
        ),
    ]
    return {r.id: r for r in records}


def test_a_hypothesis_chain_is_ordered_and_each_step_says_how_the_view_counts_it() -> None:
    from evaluation.proposal_readable import causal_chains

    text = "\n".join(causal_chains(_causal_kb(), "r", {}))
    assert "H-1: 4 claims, 4 causal" in text
    assert "1. `C-10` name 1 -> name 2 — proper" in text
    assert "2. `C-11` name 2 -> name 3 — improper" in text
    # The contradicted step is not part of the chain the view reads; it is named apart, with the reason.
    assert "Hidden from the view, not part of any chain above:" in text
    assert "hidden from the view (contradicted)" in text
    assert text.index("`C-12`") > text.index("Off this chain:")
    assert "Off this chain:" in text and "`C-13` name 5 -> name 4 — improper" in text


def test_section_a_lists_the_claims_it_cites_with_their_standing() -> None:
    from evaluation.proposal_readable import causal_chains

    proposal = {"mechanism": {"text": "t", "cites": ["C-10", "C-11", "C-12", "H-1"]}}
    text = "\n".join(causal_chains(_causal_kb(), "r", proposal))
    assert "**Section A: the causal claims it cites**" in text
    section_a = text.split("**H-1")[0].split("**Section A")[1]
    assert "`C-10`" in section_a and "`C-11`" in section_a and "hidden from the view (contradicted)" in section_a


def test_a_hypothesis_of_another_run_is_left_out_unless_the_proposal_cites_it() -> None:
    from evaluation.proposal_readable import causal_chains

    records = _causal_kb()
    assert "H-1" not in "\n".join(causal_chains(records, "other-run", {}))
    assert "H-1: 4 claims" in "\n".join(
        causal_chains(records, "other-run", {"mechanism": {"text": "t", "cites": ["H-1"]}})
    )


def test_a_run_with_no_hypothesis_says_so_rather_than_printing_nothing() -> None:
    from evaluation.proposal_readable import causal_chains

    assert any("no live hypothesis" in line for line in causal_chains({}, "r", {}))
