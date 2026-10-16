"""Agent proposals -> KB records, and the deterministic provenance check."""

from __future__ import annotations

import json

import pytest
from kb_factories import NOW, claim, entity, hypothesis
from pydantic import ValidationError

from knowledge_base.schemas import Claim, Entity, Hypothesis, Ignorance, Relation
from knowledge_base.status import statuses
from knowledge_base.store import KnowledgeBase, read_records
from knowledge_base.validate import problems
from pipeline.orchestrator import OUTPUT_MODELS
from pipeline.proposals import (
    Builder,
    DesignOutput,
    ExecuteOutput,
    GenerateOutput,
    LiteratureOutput,
    ProposedClaim,
    ReassessOutput,
    ReviewOutput,
    SynthesizeOutput,
    agent_schema,
    drop_invalid_claims,
)
from pipeline.provenance import QuoteVerifier, fetch_missing
from pipeline.synthesis import gate
from tools.literature.store import LiteratureStore, recovered_source

TEXT = (
    "Autoimmune responses are thought to have a key role in ALS pathology. C9orf72-specific CD4+ T cell "
    "responses were increased in patients with ALS. "
) * 10
QUOTE = "C9orf72-specific CD4+ T cell responses were increased in patients with ALS"


@pytest.fixture()
def store(tmp_path):
    lit = LiteratureStore(tmp_path / "lit", clock=lambda: "2026-10-02T12:00:00Z")
    lit.add(
        identifiers=["doi:10.1000/michaelis"],
        title="Michaelis",
        year=2025,
        text=TEXT,
        source_url=None,
        licence="cc by",
        extractor="test",
        run_id="r",
        alias="michaelis-2025",
    )
    return lit


def literature_output(**claim):
    base = {
        "ref": "c1",
        "type": "observational",
        "content": "C9orf72-specific CD4 responses are increased in ALS.",
        "source": "doi:10.1000/michaelis",
        "quote": QUOTE,
        "entities": ["C9orf72"],
    }
    return LiteratureOutput.model_validate({"claims": [{**base, **claim}]})


def build(tmp_path, verifier=None, known=None):
    counters: dict[str, int] = {}

    def allocate(kind):
        counters[kind] = counters.get(kind, 0) + 1
        return {"claim": "C", "entity": "N", "hypothesis": "H", "ignorance": "I", "relation": "L"}[
            kind
        ] + f"-{counters[kind]}"

    kwargs = {"verifier": verifier} if verifier else {}
    return Builder(known or {}, allocate, run_id="RUN-1", agent_id="literature-1", now=NOW, **kwargs)


def test_agent_schemas_are_self_contained_objects():
    for model in (LiteratureOutput, GenerateOutput):
        schema = agent_schema(model)
        assert schema["type"] == "object" and "$ref" not in json.dumps(schema) and "$defs" not in schema


def test_every_phase_schema_declares_what_it_requires():
    """A field stripped from `properties` but left in `required`, with `additionalProperties: false`, makes the
    sub-schema unsatisfiable and the agent never starts."""

    def check(node, path):
        if isinstance(node, dict):
            declared = set(node.get("properties", {}))
            missing = set(node.get("required", [])) - declared
            assert not missing, f"{path}: required but not declared: {sorted(missing)}"
            for name, sub in node.get("properties", {}).items():
                check(sub, f"{path}/properties/{name}")
            for key in ("items", "additionalProperties"):
                if isinstance(node.get(key), dict):
                    check(node[key], f"{path}/{key}")
            for key in ("anyOf", "oneOf", "allOf"):
                for i, sub in enumerate(node.get(key, [])):
                    check(sub, f"{path}/{key}/{i}")

    for phase, model in OUTPUT_MODELS.items():
        check(agent_schema(model), phase)


def test_a_property_named_title_survives_schema_generation():
    schema = agent_schema(DesignOutput)
    assert "title" in schema["properties"]["experiments"]["items"]["properties"]


DIGEST = "7d531cfe240d24ff04a2920f71ab740679ee0f37ebfde6e696229c0c6fed41c1"


def _reassessment(digest: str) -> dict:
    return {
        "experiment_id": "EXP-87",
        "result_digest": digest,
        "decision": "inconclusive",
        "rationale": "Four predicted alleles cannot settle the broader causal claim.",
        "claim_ids": ["C-889"],
        "limitations": ["No observed presentation."],
    }


def _execution(digest: str) -> dict:
    return {
        "experiment_id": "EXP-87",
        "analysis_id": "hla_panel_compact_v1",
        "result_digest": digest,
        "outcome": "panel_condition_met",
        "interpretation": "supports_panel_condition_only",
        "limitations": ["Predicted binding only."],
    }


@pytest.mark.parametrize("model, payload", [(ReassessOutput, _reassessment), (ExecuteOutput, _execution)])
def test_a_reported_digest_is_a_digest_and_not_a_sentence(model, payload):
    """One refusal cause, seen in a real run: the reassessor wrote the digest with a parenthesis appended, so
    the host's character-for-character comparison failed. The field was typed as free text, and neither the
    schema the agent is handed nor its prompt said otherwise."""
    assert model.model_validate(payload(DIGEST)).result_digest == DIGEST
    with pytest.raises(ValidationError, match="result_digest"):
        model.model_validate(payload(f"{DIGEST} (outcome: panel_condition_met; host interpretation_check)"))
    for wrong in ("", DIGEST.upper(), DIGEST[:63], DIGEST + "a", "not a digest"):
        with pytest.raises(ValidationError, match="result_digest"):
            model.model_validate(payload(wrong))
    # Surrounding whitespace is not a refusal: `_Proposal` strips strings before the pattern is applied.
    assert model.model_validate(payload(f"  {DIGEST}\n")).result_digest == DIGEST


@pytest.mark.parametrize("model", [ReassessOutput, ExecuteOutput])
def test_the_digest_constraint_reaches_the_contract_the_agent_is_handed(model):
    """A validator alone would only refuse after the fact; `agent_schema` keeps `pattern`, so the agent reads
    the rule in its own schema before answering."""
    assert agent_schema(model)["properties"]["result_digest"]["pattern"] == r"^[0-9a-f]{64}$"


def test_literature_claim_with_an_exact_quote_is_verified(tmp_path, store):
    verifier = QuoteVerifier(store, tmp_path / "calls.jsonl")
    batch = build(tmp_path, verifier).literature(literature_output())
    claim = next(r for r in batch.records if isinstance(r, Claim))
    assert (claim.origin, claim.provenance_status, claim.verification_method) == ("literature", "verified", "quote")
    assert claim.source_refs[0] == "doi:10.1000/michaelis" and claim.source_refs[1].startswith("receipt:")
    assert claim.quote == QUOTE and batch.refs == {"c1": "C-1"}
    assert [type(r) for r in batch.records] == [Entity, Claim]  # entities first


@pytest.mark.parametrize(
    "change",
    [{"quote": "CD8 T cells were decreased in every patient"}, {"source": "doi:10.1000/not-in-store"}, {"quote": None}],
)
def test_anything_short_of_an_exact_quote_stays_unverified(tmp_path, store, change):
    verifier = QuoteVerifier(store, tmp_path / "calls.jsonl")
    claim = next(
        r for r in build(tmp_path, verifier).literature(literature_output(**change)).records if isinstance(r, Claim)
    )
    assert (claim.provenance_status, claim.verification_method) == ("unverified", "none")


def test_a_sourceless_literature_claim_is_recorded_as_agent_knowledge(tmp_path):
    claim = next(
        r
        for r in build(tmp_path).literature(literature_output(source=None, quote=None)).records
        if isinstance(r, Claim)
    )
    assert claim.origin == "agent"


def test_known_entities_are_reused_by_name(tmp_path):
    known = {"N-7": entity(id="N-7", canonical_name="c9orf72")}
    batch = build(tmp_path, known=known).literature(literature_output())
    assert not any(isinstance(r, Entity) for r in batch.records)
    assert next(r for r in batch.records if isinstance(r, Claim)).entity_refs == ["N-7"]


def test_an_agent_cannot_type_a_claim_causal_and_may_always_attach_the_block():
    """The enum does not offer `causal`, so a claim typed causal yet observational cannot be written; the
    block is independent of the type and carries the edge."""
    with pytest.raises(ValidationError, match="observational"):
        ProposedClaim(ref="c1", type="causal", content="x drives y")

    both = ProposedClaim(
        ref="c1",
        type="observational",
        content="x impairs y",
        causal={"cause": "x", "effect": "y", "context": "ALS", "mechanism": "m"},
    )
    assert both.type == "observational" and both.causal is not None
    assert "causal" not in agent_schema(LiteratureOutput)["properties"]["claims"]["items"]["properties"]["type"]["enum"]


GENERATED = {
    "hypothesis": {
        "statement": "Loss of tolerance to C9orf72 drives neurodegeneration",
        "mechanism": "Autoreactive CD4 T cells against C9orf72 damage motor neurons.",
        "author_confidence": 2,
        "claims": [
            {
                "ref": "c1",
                "type": "observational",
                "content": "Autoreactive CD4 T cells drive motor neuron loss",
                "causal": {
                    "cause": "C9orf72-specific CD4 T cells",
                    "effect": "motor neuron loss",
                    "context": "C9-ALS",
                    "mechanism": "cytotoxic cytokines",
                },
            },
            {"ref": "c2", "type": "assumption", "content": "Peripheral T cells reach the CNS."},
        ],
        "links": [
            {"type": "supports", "source": "C-1", "target": "c1", "rationale": "responses are increased"},
            {"type": "depends_on", "source": "c1", "target": "c2", "rationale": "needs CNS access"},
        ],
        "ignorance": [{"content": "Is the response cause or consequence?", "questions": ["c1"]}],
    }
}


def test_generated_hypothesis_becomes_linked_records_in_the_kb(tmp_path, store):
    kb_root = tmp_path / "kb"
    with KnowledgeBase.open(kb_root, clock=lambda: NOW) as kb:
        lit = Builder(
            kb.records, kb.allocate, "RUN-1", "literature-1", NOW, QuoteVerifier(store, tmp_path / "calls.jsonl")
        ).literature(literature_output())
        assert kb.commit(lit.records, "RUN-1").rejected == {}
        gen = Builder(kb.records, kb.allocate, "RUN-1", "author-1", NOW).hypothesis(
            GenerateOutput.model_validate(GENERATED)
        )
        result = kb.commit(gen.records, "RUN-1")
    assert result.rejected == {}
    records = read_records(kb_root)
    [hyp] = [r for r in records.values() if isinstance(r, Hypothesis)]
    [gap] = [r for r in records.values() if isinstance(r, Ignorance)]
    relations = {(r.type, r.source_id, r.target_id) for r in records.values() if isinstance(r, Relation)}
    c1, c2 = gen.refs["c1"], gen.refs["c2"]
    assert {
        ("part_of", c1, hyp.id),
        ("part_of", c2, hyp.id),
        ("supports", "C-1", c1),
        ("depends_on", c1, c2),
        ("questions", gap.id, c1),
    } == relations
    support = next(r for r in records.values() if isinstance(r, Relation) and r.type == "supports")
    assert support.source_refs[0] == "doi:10.1000/michaelis" and support.rationale == "responses are increased"
    assert support.provenance_status == "unverified"  # an agent's inference never weighs


def test_a_link_to_an_unknown_local_ref_is_skipped_with_its_reason(tmp_path):
    bad = json.loads(json.dumps(GENERATED))
    bad["hypothesis"]["links"] = [{"type": "depends_on", "source": "c1", "target": "c9", "rationale": "r"}]
    batch = build(tmp_path).hypothesis(GenerateOutput.model_validate(bad))
    assert batch.skipped == ["depends_on c1 -> c9: unknown local ref c9"]
    assert not any(isinstance(r, Relation) and r.type == "depends_on" for r in batch.records)


# --- a refused `contradicts` becomes a declared gap ------------------------------------------------------

# An objection whose evidence claim is unverified: the KB refuses the relation, and without this path the
# objection would be dropped with it.
OBJECTION = (
    "The protective role of IL-5 in clinical ALS contradicts the mechanistic assumption that IL-5-driven "
    "eosinophilic infiltration mediates motor-neuron toxicity."
)


def review_output(**over):
    base = {
        "summary": "IL-5 looks protective, not pathogenic.",
        "claims": [
            {"ref": "c1", "type": "observational", "content": "IL-5 levels correlate with slower progression in ALS."}
        ],
        "links": [{"type": "contradicts", "source": "c1", "target": "C-100", "rationale": OBJECTION}],
        "gaps": [],
    }
    return ReviewOutput.model_validate({**base, **over})


def proposal(cites=("C-100", "H-1")):
    section = {"text": "See [[C-100]] and [[H-1]].", "cites": list(cites)}
    return SynthesizeOutput.model_validate(
        {
            "outcome": "insufficient_evidence",
            "summary": "s",
            **{
                k: section
                for k in (
                    "mechanism",
                    "therapeutic_hypothesis",
                    "intervention",
                    "stratification",
                    "biomarker",
                    "validation",
                    "translation",
                )
            },
            "failure_modes": [{"text": f"f{n}", "cites": []} for n in range(5)],
        }
    )


def test_a_contradicts_from_an_unverified_claim_becomes_a_gap_that_questions_the_same_target(tmp_path):
    known = {"C-100": claim(id="C-100"), "H-1": hypothesis(id="H-1")}
    batch = build(tmp_path, known=known).review(review_output(), "H-1")
    [gap] = [r for r in batch.records if isinstance(r, Ignorance)]
    [relation] = [r for r in batch.records if isinstance(r, Relation)]
    assert gap.hypothesis_id == "H-1" and gap.agent_id == "literature-1"
    assert OBJECTION in gap.content and "contradicts" in gap.content  # the rationale, and what it was
    assert (relation.type, relation.source_id, relation.target_id) == ("questions", gap.id, "C-100")
    assert relation.rationale == OBJECTION
    assert not any(isinstance(r, Relation) and r.type == "contradicts" for r in batch.records)


def test_the_conversion_is_reported_as_converted_and_not_as_dropped(tmp_path):
    batch = build(tmp_path, known={"C-100": claim(id="C-100"), "H-1": hypothesis(id="H-1")}).review(
        review_output(), "H-1"
    )
    [note] = batch.converted
    assert batch.skipped == []
    assert note.startswith("contradicts") and "C-100" in note and "not verified" in note


def test_the_converted_pair_passes_the_validation_that_refused_the_contradicts(tmp_path):
    known = {"C-100": claim(id="C-100"), "H-1": hypothesis(id="H-1")}
    batch = build(tmp_path, known=known).review(review_output(), "H-1")
    seen = dict(known)
    for record in batch.records:  # the order the store commits in
        assert problems(record, seen) == []
        seen[record.id] = record


def test_a_contradicts_from_a_verified_claim_is_still_a_contradicts(tmp_path):
    known = {
        "C-100": claim(id="C-100"),
        "H-1": hypothesis(id="H-1"),
        "C-200": claim(id="C-200", content="Eosinophils are absent from ALS CNS tissue."),
    }
    output = review_output(
        links=[{"type": "contradicts", "source": "C-200", "target": "C-100", "rationale": OBJECTION}]
    )
    batch = build(tmp_path, known=known).review(output, "H-1")
    [relation] = [r for r in batch.records if isinstance(r, Relation)]
    assert (relation.type, relation.source_id, relation.target_id) == ("contradicts", "C-200", "C-100")
    assert relation.source_refs == ["doi:10.1038/x"] and batch.converted == []
    assert not any(isinstance(r, Ignorance) for r in batch.records)


def test_the_conversion_moves_neither_the_epistemic_status_nor_the_gate(tmp_path):
    """A `questions` relation is not negative evidence (`status.statuses` weighs supports/contradicts only), so
    the conversion asserts strictly less than the refused objection did."""
    known = {"C-100": claim(id="C-100"), "H-1": hypothesis(id="H-1")}
    before = gate(proposal(), known, "RUN-1")
    batch = build(tmp_path, known=known).review(review_output(), "H-1")
    after = {**known, **{r.id: r for r in batch.records}}
    assert statuses(known)["C-100"] == statuses(after)["C-100"] == "unresolved"
    assert gate(proposal(), after, "RUN-1") == before


def test_a_generated_hypothesis_converts_its_own_refused_objection_too(tmp_path):
    """The `generate` phase links to the KB slice as well; the gap is attached to the hypothesis being written."""
    output = json.loads(json.dumps(GENERATED))
    output["hypothesis"]["links"] = [{"type": "contradicts", "source": "c2", "target": "C-100", "rationale": OBJECTION}]
    batch = build(tmp_path, known={"C-100": claim(id="C-100")}).hypothesis(GenerateOutput.model_validate(output))
    [hyp] = [r for r in batch.records if isinstance(r, Hypothesis)]
    [gap] = [r for r in batch.records if isinstance(r, Ignorance) and "contradicts" in r.content]
    assert gap.hypothesis_id == hyp.id and OBJECTION in gap.content
    assert ("questions", gap.id, "C-100") in {
        (r.type, r.source_id, r.target_id) for r in batch.records if isinstance(r, Relation)
    }
    assert len(batch.converted) == 1 and batch.skipped == []


def test_fetch_missing_asks_only_for_absent_papers(store):
    asked: list[str] = []

    def transport(url: str) -> str:
        asked.append(url)
        return '{"resultList": {"result": []}}'

    outcome = fetch_missing(
        ["doi:10.1000/michaelis", "doi:10.1000/other"], store, "RUN-1", max_new=5, transport=transport
    )
    assert list(outcome) == ["doi:10.1000/other"] and asked and all("michaelis" not in u for u in asked)
    # The value is {outcome, reason}: `paywalled` alone cannot be acted on, so the run's
    # archive keeps the sentence the fetcher recorded beside the bucket.
    assert fetch_missing(["doi:10.1000/x"], store, "RUN-1", max_new=0) == {
        "doi:10.1000/x": {"outcome": "not_fetched", "reason": "the phase's fetch budget was already spent"}
    }


def _design_output(**over):
    from pipeline.proposals import DesignOutput

    experiment = {
        "title": "Reanalyse the epitope table",
        "protocol": "Recompute immunodominance over Table 1.",
        "tested_claims": ["C-1"],
        "expected_outcomes": [
            {"outcome": "one epitope dominates", "claim_id": "C-1", "effect": "supports"},
            {"outcome": "none does", "claim_id": "C-1", "effect": "contradicts"},
        ],
        "cost": "low",
        "cost_rationale": "data on hand",
        "feasibility": "high",
        **over,
    }
    return DesignOutput.model_validate({"experiments": [experiment]})


def test_an_in_silico_experiment_on_a_declared_dataset_is_written(tmp_path):
    """A dataset is the instrument of a proposed experiment."""
    from knowledge_base.schemas import Experiment

    counters: dict[str, int] = {}

    def allocate(kind):
        counters[kind] = counters.get(kind, 0) + 1
        return {"experiment": "EXP", "relation": "L"}[kind] + f"-{counters[kind]}"

    builder = Builder({"C-1": claim(id="C-1")}, allocate, "RUN-1", "designer-1", NOW)
    batch = builder.experiments(
        _design_output(
            kind="in_silico", dataset_refs=["dataset:michaelis-table1-epitopes@89af13c3630a"], dataset_location="local"
        ),
        "H-1",
        datasets={"michaelis-table1-epitopes"},
    )
    [written] = [r for r in batch.records if isinstance(r, Experiment)]
    assert written.kind == "in_silico" and written.dataset_location == "local"
    assert batch.skipped == []


def test_an_experiment_claiming_a_dataset_the_run_does_not_have_is_skipped():
    counters: dict[str, int] = {}

    def allocate(kind):
        counters[kind] = counters.get(kind, 0) + 1
        return {"experiment": "EXP", "relation": "L"}[kind] + f"-{counters[kind]}"

    builder = Builder({"C-1": claim(id="C-1")}, allocate, "RUN-1", "designer-1", NOW)
    batch = builder.experiments(
        _design_output(kind="in_silico", dataset_refs=["some-cohort-nobody-declared"], dataset_location="local"),
        "H-1",
        datasets={"michaelis-table1-epitopes"},
    )
    # Calling a dataset local that the manifest does not hold claims a provenance nobody can check.
    assert batch.records == [] and "not in this run's manifest" in batch.skipped[0]


def test_a_remote_dataset_is_not_checked_against_the_manifest():
    counters: dict[str, int] = {}

    def allocate(kind):
        counters[kind] = counters.get(kind, 0) + 1
        return {"experiment": "EXP", "relation": "L"}[kind] + f"-{counters[kind]}"

    from knowledge_base.schemas import Experiment

    builder = Builder({"C-1": claim(id="C-1")}, allocate, "RUN-1", "designer-1", NOW)
    batch = builder.experiments(
        _design_output(kind="in_silico", dataset_refs=["GCST90027164"], dataset_location="remote"),
        "H-1",
        datasets={"michaelis-table1-epitopes"},
    )
    [written] = [r for r in batch.records if isinstance(r, Experiment)]
    assert written.dataset_location == "remote" and written.dataset_refs == ["GCST90027164"]


# --- A confirmed verdict on an overstated claim (`Builder.narrowed`) ------------------------------------------
# Without this path, `Builder.confirmed` demands `faithful` and the whole verdict is dropped, including any
# confirmed contradiction, so no hypothesis could become `contradicted` through an overstated claim.

OVERSTATED = (
    "IL-5 levels correlate negatively with ALS progression, suggesting a protective rather than pathogenic role."
)
NARROW = "A negative correlation between IL-5 levels and the rate of disease progression was identified."
ITS_QUOTE = "identified a negative correlation between IL-5 levels and the rate of disease progression"


def _verdict(**overrides):
    from pipeline.proposals import Verdict

    fields = {
        "relation_id": "L-1",
        "faithful": False,
        "verdict": "confirmed",
        "reason": "the quote shows the correlation; 'protective' is the claim's own interpretation",
        "narrowed": NARROW,
    }
    fields.update(overrides)
    return Verdict(**fields)


def _to_narrow(source=None, rtype="contradicts", verified_quote=True):
    """As a live run offers it: `_verify_candidates` only hands over relations whose **source claim is already
    verified**, so the overstated claim is verified evidence and the verdict says it overstates its own quote."""
    from pipeline.proposals import Provenance

    source = source or claim(
        id="C-1", content=OVERSTATED, quote=ITS_QUOTE, source_refs=["doi:10.1038/x", "receipt:old"]
    )
    target = claim(id="C-2", content="IL-5 drives neuroinflammation in ALS.")
    relation = Relation(
        id="L-1",
        source_id=source.id,
        target_id=target.id,
        type=rtype,
        justification_method="agent_inference",
        provenance_status="unverified",
        source_refs=["doi:10.1038/x"],
        run_id="RUN-1",
        created_at=NOW,
    )
    known = {source.id: source, target.id: target, relation.id: relation}

    counters: dict[str, int] = {}

    def allocate(kind):
        counters[kind] = counters.get(kind, 0) + 1
        return {"claim": "C", "relation": "L", "ignorance": "I"}[kind] + f"-{10 + counters[kind]}"

    def verifier(proposed):
        return Provenance("verified", "quote", ("receipt:new",)) if verified_quote else Provenance("unverified", "none")

    return Builder(known, allocate, "RUN-1", "verifier-1", NOW, verifier=verifier), relation, known


def test_a_confirmed_verdict_on_an_overstated_claim_keeps_the_evidence_in_a_narrower_claim():
    builder, relation, known = _to_narrow()

    batch = builder.narrowed(relation, _verdict())

    narrow = next(r for r in batch.records if isinstance(r, Claim))
    assert narrow.content == NARROW
    assert (narrow.provenance_status, narrow.verification_method) == ("verified", "quote")
    assert narrow.source_refs == ["doi:10.1038/x", "receipt:new"]  # its own receipt, not the original's
    assert narrow.quote == ITS_QUOTE
    relations = {r.type: r for r in batch.records if isinstance(r, Relation)}
    assert set(relations) == {"derived_from", "contradicts", "questions"}
    assert relations["derived_from"].target_id == "C-1"  # the lineage, not a correction in place
    assert relations["contradicts"].source_id == narrow.id and relations["contradicts"].target_id == "C-2"
    assert all(
        r.provenance_status == "verified" and r.justification_method == "quote"
        for r in (relations["derived_from"], relations["contradicts"])
    )
    assert "narrowed to the quote" in relations["contradicts"].rationale
    assert any("overstated" in line for line in batch.converted)  # reported as converted, not as dropped
    assert not problems(narrow, known)


def test_the_overstatement_is_questioned_and_left_as_it_was():
    """It stays `verified`, because `verified` only ever meant its quote is in the paper. What the verifier found
    is recorded as an `Ignorance`, the same demotion an unverified objection gets from `_as_question`."""
    builder, relation, known = _to_narrow()

    batch = builder.narrowed(relation, _verdict())

    gap = next(r for r in batch.records if isinstance(r, Ignorance))
    assert "says more than its quote" in gap.content and "C-1" in gap.content
    questions = next(r for r in batch.records if isinstance(r, Relation) and r.type == "questions")
    assert (questions.source_id, questions.target_id) == (gap.id, "C-1")
    assert questions.provenance_status == "unverified"  # a question is never negative evidence
    assert known["C-1"].provenance_status == "verified"  # untouched, as it was offered


def test_the_narrowed_claim_makes_the_target_contradicted():
    builder, relation, known = _to_narrow()
    batch = builder.narrowed(relation, _verdict())

    records = known | {r.id: r for r in batch.records}
    assert statuses(records)["C-2"] == "contradicted"


@pytest.mark.parametrize(
    "verdict, expected",
    [
        (_verdict(faithful=True), "narrowing applies to a confirmed verdict on an unfaithful claim"),
        (_verdict(verdict="insufficient"), "narrowing applies to a confirmed verdict on an unfaithful claim"),
        (_verdict(narrowed=None), "no restatement was offered"),
        (_verdict(narrowed=OVERSTATED), "repeats C-1, an endpoint of the relation"),
        (_verdict(narrowed=f"  {OVERSTATED.upper()} "), "repeats C-1, an endpoint of the relation"),
        (_verdict(narrowed="IL-5 drives neuroinflammation in ALS."), "repeats C-2, an endpoint of the relation"),
    ],
)
def test_narrowing_is_refused_with_a_reason(verdict, expected):
    builder, relation, _ = _to_narrow()
    with pytest.raises(ValueError, match=expected):
        builder.narrowed(relation, verdict)


def test_a_restatement_whose_quote_cannot_be_verified_is_refused():
    """The narrowed claim passes the same provenance check as any other: no receipt, no verified evidence."""
    builder, relation, _ = _to_narrow(verified_quote=False)
    with pytest.raises(ValueError, match="could not be verified against doi:10.1038/x"):
        builder.narrowed(relation, _verdict())


def test_a_causal_claim_is_not_narrowed_by_restating_its_prose():
    """Its overstatement can sit in the cause, effect or context, which a prose restatement leaves intact."""
    from kb_factories import CAUSAL

    causal = claim(id="C-1", type="causal", content=OVERSTATED, quote=ITS_QUOTE, **CAUSAL)
    builder, relation, _ = _to_narrow(source=causal)
    with pytest.raises(ValueError, match="cannot be narrowed by restating its prose alone"):
        builder.narrowed(relation, _verdict())


def test_a_restatement_that_repeats_an_unverified_claim_is_refused():
    """The store would alias it to that claim, so the "verified" relation would rest on unverified evidence.
    Converging on a verified claim is fine; converging on an unverified one is not."""
    loose = claim(
        id="C-3",
        content="A claim nobody verified.",
        provenance_status="unverified",
        verification_method="none",
        origin="agent",
        source_refs=[],
    )
    builder, relation, known = _to_narrow()
    known[loose.id] = loose

    with pytest.raises(ValueError, match="repeats C-3, which is not verified"):
        builder.narrowed(relation, _verdict(narrowed="A claim nobody verified."))


def test_a_restatement_that_meets_a_verified_claim_of_another_run_is_allowed():
    """Convergence on evidence an earlier run already established is the shared knowledge base working: the
    store aliases the claim and the relation is redirected to it."""
    earlier = claim(id="C-9", content="A narrower statement an earlier run already made.", run_id="RUN-0")
    builder, relation, known = _to_narrow()
    known[earlier.id] = earlier

    batch = builder.narrowed(relation, _verdict(narrowed=earlier.content))

    assert [r.content for r in batch.records if isinstance(r, Claim)] == [earlier.content]


# --- Two fields a designer gets wrong mechanically (`Builder._normalised`) ------------------------------------
# An absent `dataset_location`, or an outcome about a claim not listed as tested, would otherwise drop a
# proposed experiment for a reason that is not about the science.


def _design_builder(known=None):
    counters: dict[str, int] = {}

    def allocate(kind):
        counters[kind] = counters.get(kind, 0) + 1
        return {"experiment": "EXP", "relation": "L"}[kind] + f"-{counters[kind]}"

    return Builder(known or {"C-1": claim(id="C-1")}, allocate, "RUN-1", "designer-1", NOW)


def test_a_missing_dataset_location_is_derived_from_the_run_manifest():
    """It is a fact about the run, not an opinion: what the manifest holds is local, the rest must be acquired."""
    from knowledge_base.schemas import Experiment

    builder = _design_builder()
    batch = builder.experiments(
        _design_output(kind="in_silico", dataset_refs=["dataset:michaelis-table1-epitopes@89af13c3630a"]),
        "H-1",
        datasets={"michaelis-table1-epitopes"},
    )

    [written] = [r for r in batch.records if isinstance(r, Experiment)]
    assert written.dataset_location == "local" and batch.skipped == []
    assert any("derived local from this run's manifest" in line for line in batch.converted)


def test_a_dataset_the_run_does_not_hold_is_derived_as_remote_instead_of_dropped():
    from knowledge_base.schemas import Experiment

    builder = _design_builder()
    batch = builder.experiments(
        _design_output(kind="in_silico", dataset_refs=["GSE123456"]), "H-1", datasets={"michaelis-table1-epitopes"}
    )

    [written] = [r for r in batch.records if isinstance(r, Experiment)]
    assert written.dataset_location == "remote"  # it would have to be acquired, which is honest
    assert batch.skipped == [] and any("derived remote" in line for line in batch.converted)


def test_an_outcome_about_an_unlisted_claim_adds_it_to_the_tested_claims():
    """An outcome about a claim says the experiment bears on it; that is read as what it says, and the `tests`
    relation follows, instead of the whole experiment being refused."""
    from knowledge_base.schemas import Experiment

    builder = _design_builder(known={"C-1": claim(id="C-1"), "C-2": claim(id="C-2", content="Another claim.")})
    batch = builder.experiments(
        _design_output(
            expected_outcomes=[
                {"outcome": "one epitope dominates", "claim_id": "C-1", "effect": "supports"},
                {"outcome": "none does", "claim_id": "C-2", "effect": "contradicts"},
            ]
        ),
        "H-1",
    )

    [written] = [r for r in batch.records if isinstance(r, Experiment)]
    assert written.tested_claims == ["C-1", "C-2"]
    assert {r.target_id for r in batch.records if isinstance(r, Relation) and r.type == "tests"} == {"C-1", "C-2"}
    assert any("added to tested_claims" in line for line in batch.converted)
    assert batch.skipped == []


def test_an_explicit_local_on_a_dataset_the_run_lacks_is_still_refused():
    """Deriving the field does not weaken the check above it: a claimed provenance nobody can check is skipped."""
    builder = _design_builder()
    batch = builder.experiments(
        _design_output(kind="in_silico", dataset_refs=["some-cohort-nobody-declared"], dataset_location="local"),
        "H-1",
        datasets={"michaelis-table1-epitopes"},
    )

    assert batch.records == [] and "not in this run's manifest" in batch.skipped[0]


# --- the host canonicalises the references the agent wrote ----------------------------------------
# The agent is neither asked nor trusted to use one form: it never writes to the knowledge base, so the
# host does it when it builds the record. These assert that the host actually calls the one
# canonicaliser (`tools.literature.store`), not that the function works -- that is its own test.


def test_the_host_puts_an_agent_s_source_reference_into_canonical_form(tmp_path):
    claim = next(
        r
        for r in build(tmp_path).literature(literature_output(source="DOI: 10.1000/MICHAELIS", quote=None)).records
        if isinstance(r, Claim)
    )
    assert claim.source_refs[0] == "doi:10.1000/michaelis"


def test_the_form_the_agent_wrote_is_kept_beside_the_canonical_one(tmp_path):
    """Provenance is about what was said, not about what we made of it."""
    claim = next(
        r
        for r in build(tmp_path).literature(literature_output(source="PMID:29542041", quote=None)).records
        if isinstance(r, Claim)
    )
    assert claim.source_refs[0] == "pmid:29542041"
    assert claim.source_refs_raw == ["PMID:29542041"]


def test_a_reference_already_canonical_leaves_no_raw_duplicate(tmp_path):
    claim = next(
        r
        for r in build(tmp_path).literature(literature_output(source="doi:10.1000/michaelis", quote=None)).records
        if isinstance(r, Claim)
    )
    assert claim.source_refs == ["doi:10.1000/michaelis"]
    assert claim.source_refs_raw == []


@pytest.mark.parametrize("bad", ["Web search results", "C-111", "alsnewstoday.com (ALS News Today)", "41034581"])
def test_a_source_that_does_not_declare_its_kind_is_refused(bad):
    """All four are real `source_refs` from production runs, and none can be looked up as written: a
    sentence, a claim id, a news domain and a bare number. The structured-output contract refuses them,
    which is stronger than asking in a prompt.

    `41034581` is the one that must keep failing. It is a real PMID, and reading it as one is exactly the
    guess the declaration rule retired: five to nine digits identify nothing by themselves.
    """
    with pytest.raises(ValidationError):
        literature_output(source=bad)


@pytest.mark.parametrize(
    "bare,canonical",
    [
        ("10.1000/michaelis", "doi:10.1000/michaelis"),
        ("PMC1234567", "pmcid:PMC1234567"),
        ("pubmed.ncbi.nlm.nih.gov/29542041", "pmid:29542041"),
    ],
)
def test_a_source_that_identifies_itself_by_shape_is_recovered(bare, canonical):
    """A bare DOI or PMCID declares its kind by its shape, so refusing it serves neither purpose of the
    rule -- it is diagnosable, and reading it needs no guess. Refusing it would cost a claim its attribution
    over a missing four-character prefix.
    """
    assert literature_output(source=bare).claims[0].source == bare
    assert recovered_source(bare) == canonical


def test_the_host_reports_a_recovered_source_rather_than_accepting_it_silently(tmp_path):
    """The host recognising is not the agent declaring. The claim keeps its literature origin, the
    canonical form goes to `source_refs`, the agent's spelling stays in `source_refs_raw`, and the
    recognition is named in the batch's conversions -- so an audit of whether the agents ever learned the
    rule can still tell the two apart.
    """
    proposed = ProposedClaim.model_construct(
        ref="c1",
        type="observational",
        content="A claim whose source names itself.",
        source="10.1000/michaelis",
        quote=None,
        entities=[],
        causal=None,
    )
    builder = build(tmp_path)
    builder.claim(proposed, origin="literature")
    claim = next(r for r in builder.batch.records if isinstance(r, Claim))
    assert claim.origin == "literature"
    assert claim.source_refs == ["doi:10.1000/michaelis"]
    assert claim.source_refs_raw == ["10.1000/michaelis"]
    assert any("recognised by the host as doi:10.1000/michaelis" in c for c in builder.batch.converted)


@pytest.mark.parametrize(
    "good",
    [
        "doi:10.1000/michaelis",
        "PMID:29542041",
        "PMCID:PMC1234567",
        "arxiv:2401.01234",
        "https://doi.org/10.1000/michaelis",
    ],
)
def test_a_source_that_declares_its_kind_is_accepted(good):
    assert literature_output(source=good).claims[0].source == good


def test_an_undeclared_source_reaching_the_host_demotes_the_claim(tmp_path):
    """Defence in depth behind the validator. Nothing is discarded: the string the agent wrote is kept,
    and the claim is recorded as the system's own reasoning rather than as literature, because nobody
    could look its source up."""
    proposed = ProposedClaim.model_construct(
        ref="c1",
        type="observational",
        content="A claim with a source nobody can chase.",
        source="Web search results",
        quote=None,
        entities=[],
        causal=None,
    )
    builder = build(tmp_path)
    builder.claim(proposed, origin="literature")
    claim = next(r for r in builder.batch.records if isinstance(r, Claim))
    assert claim.origin == "agent"
    assert claim.source_refs == []
    assert claim.source_refs_raw == ["Web search results"]


# --- an undeclared source costs the attribution, not the claim -----------------------------------


def test_a_claim_whose_only_fault_is_the_source_is_repaired_not_dropped():
    """The validator refuses an undeclared source. Dropping the claim would lose content that was not
    what was wrong -- the attribution was. It is kept with no source, which the builder then records as
    the system's own reasoning."""
    raw = {
        "claims": [
            {
                "ref": "c1",
                "type": "observational",
                "content": "A real finding.",
                "source": "Web search results",
                "quote": None,
                "entities": [],
            }
        ]
    }
    cleaned, dropped, repaired = drop_invalid_claims(raw)
    assert dropped == []
    assert cleaned["claims"][0]["source"] is None
    assert cleaned["claims"][0]["content"] == "A real finding."
    assert "does not declare its kind" in repaired[0]


def test_a_claim_broken_for_another_reason_is_still_dropped():
    """The repair is narrow on purpose: it removes an attribution, never invents a field."""
    raw = {"claims": [{"ref": "c1", "type": "nonsense", "content": "x", "source": "doi:10.1/x"}]}
    cleaned, dropped, repaired = drop_invalid_claims(raw)
    assert cleaned["claims"] == []
    assert dropped and not repaired


def test_a_repaired_claim_reaches_the_knowledge_base_as_agent_knowledge(tmp_path):
    raw = {
        "claims": [
            {
                "ref": "c1",
                "type": "observational",
                "content": "A real finding.",
                "source": "alsnewstoday.com",
                "quote": None,
                "entities": [],
            }
        ]
    }
    cleaned, _, _ = drop_invalid_claims(raw)
    claim = next(
        r for r in build(tmp_path).literature(LiteratureOutput.model_validate(cleaned)).records if isinstance(r, Claim)
    )
    assert claim.origin == "agent"
    assert claim.source_refs == []


# --- a relation inherits its source claim's references, in one form -------------------------------


def test_a_relation_canonicalises_the_references_it_inherits(tmp_path):
    """A relation writes no reference of its own: it copies what its source claim cites. The knowledge
    base is append-only, so a claim written before the declaration rule keeps its bare spelling forever,
    and the copy is the only point where the backlog can be put into one form.

    Nothing is lost by canonicalising here -- the agent's original spelling stays in the source claim's own
    record, which is never rewritten.
    """
    known = {
        "C-1": claim(id="C-1", source_refs=["10.1038/s41586-025-09588-6", "PMC10800391"]),
        "C-2": claim(id="C-2", content="A target claim."),
    }
    builder = build(tmp_path, known=known)
    builder.relation("supports", "C-1", "C-2")
    relation = next(r for r in builder.batch.records if isinstance(r, Relation))
    assert relation.source_refs == ["doi:10.1038/s41586-025-09588-6", "pmcid:PMC10800391"]


def test_a_relation_keeps_an_inherited_reference_that_names_no_identifier(tmp_path):
    """`Web search results` is a `source_refs` value an agent can write, and relations inherit it. No
    pattern can rescue it, and deleting it would hide that the relation rests on nothing checkable, so it is
    kept exactly as written.
    """
    known = {
        "C-1": claim(id="C-1", source_refs=["Web search results"]),
        "C-2": claim(id="C-2", content="A target claim."),
    }
    builder = build(tmp_path, known=known)
    builder.relation("supports", "C-1", "C-2")
    relation = next(r for r in builder.batch.records if isinstance(r, Relation))
    assert relation.source_refs == ["Web search results"]


# --- a `source` that is reasoning, not a reference -------------------------------------------------

# Two values an agent wrote into `source`: the author naming the knowledge-base claims it reasoned from, and
# the same plus a conflict it had noticed and could not place.
REASONED = (
    "C-232, C-505 (causal); C9orf72 loss of function is the pathophysiological consequence of " "repeat expansion"
)
FLAGGED = (
    "C-922, C-928 (synergistic IL-5 with LOF vulnerability), C-233 (secondary inflammatory damage); "
    "CONTRADICTS C-879, C-935, C-940 (claim IL-5 correlates with slower progression - unresolved "
    "contradiction)"
)


def internal(source, **over):
    raw = {
        "claims": [
            {
                "ref": "c1",
                "type": "assumption",
                "content": "A mechanistic step.",
                **({"source": source} if source is not None else {}),
                **over,
            }
        ]
    }
    cleaned, dropped, repaired = drop_invalid_claims(raw)
    assert dropped == []
    return cleaned["claims"][0], repaired


def test_a_source_that_names_knowledge_base_claims_is_kept_as_the_reasoning_it_is():
    """A source of this shape is not a citation. The field is typed for one external reference, so the host
    refuses it, and the reasoning is moved to the fields that can hold it instead of being lost."""
    repaired, notes = internal(REASONED)
    assert repaired["source"] is None
    assert repaired["derived_from"] == ["C-232", "C-505"]
    assert repaired["contradicts"] == []
    assert repaired["reasoning"] == REASONED  # verbatim: what was said, not our reading of it
    assert "derived_from C-232 C-505" in notes[0]
    assert "does not declare its kind" not in notes[0]  # a different repair, and a different metric


def test_a_flagged_contradiction_is_routed_where_contradictions_belong():
    repaired, _ = internal(FLAGGED)
    assert repaired["derived_from"] == ["C-922", "C-928", "C-233"]
    assert repaired["contradicts"] == ["C-879", "C-935", "C-940"]


def test_the_verb_contradicts_in_prose_is_not_a_flagged_conflict():
    """`CONTRADICTS C-879` marks a conflict; "which contradicts the Th2 account" states one about nothing the
    graph can hold. Only claim IDs immediately after the word are read as targets."""
    repaired, _ = internal("C-30, C-35; which contradicts the Th2 account of eosinophil recruitment")
    assert repaired["derived_from"] == ["C-30", "C-35"]
    assert repaired["contradicts"] == []


def test_a_bare_claim_id_leaves_no_rationale_that_says_nothing():
    """`'C-269'` is also a real refusal. Keeping it as `reasoning` as well would write a rationale that repeats
    the relation it annotates."""
    repaired, notes = internal("C-269")
    assert repaired["derived_from"] == ["C-269"] and repaired["reasoning"] is None
    assert "rationale" not in notes[0]


def test_a_source_naming_no_claim_keeps_the_older_repair():
    """The two repairs do not compete: a sentence that names no record still costs the attribution only."""
    repaired, notes = internal("Web search results")
    assert repaired["source"] is None and repaired.get("derived_from", []) == []
    assert "does not declare its kind" in notes[0]


def test_the_field_stays_closed_to_internal_ids_rather_than_learning_to_accept_them():
    """The repair is the host's, not a widening of the contract: `source` remains external-reference-only, so
    an author writing a claim ID there is still refused and still told where it belongs."""
    with pytest.raises(ValidationError) as err:
        ProposedClaim.model_validate({"ref": "c1", "type": "assumption", "content": "x", "source": "C-232"})
    assert "does not declare its kind" in str(err.value)


def test_reasoning_with_nothing_to_hang_it_on_is_refused():
    with pytest.raises(ValidationError):
        ProposedClaim.model_validate(
            {"ref": "c1", "type": "assumption", "content": "x", "reasoning": "Because the proteostasis argument holds."}
        )


def test_internal_reasoning_reaches_the_knowledge_base_as_relations(tmp_path):
    known = {"C-232": claim(id="C-232"), "C-505": claim(id="C-505")}
    repaired, _ = internal(REASONED)
    batch = build(tmp_path, known=known).literature(LiteratureOutput.model_validate({"claims": [repaired]}))
    [written] = [r for r in batch.records if isinstance(r, Claim)]
    relations = [r for r in batch.records if isinstance(r, Relation)]
    assert written.origin == "agent" and written.source_refs == []
    assert [(r.type, r.source_id, r.target_id) for r in relations] == [
        ("derived_from", written.id, "C-232"),
        ("derived_from", written.id, "C-505"),
    ]
    assert all(r.rationale == REASONED and r.provenance_status == "unverified" for r in relations)


def test_a_flagged_conflict_from_an_unverified_claim_becomes_a_question_like_any_other(tmp_path):
    """The routing reuses `_as_question`: an author's flag asserts no more than a reviewer's objection does,
    and it is the same ladder."""
    known = {"C-922": claim(id="C-922"), "C-879": claim(id="C-879")}
    repaired, _ = internal(FLAGGED, derived_from=[], contradicts=[])
    repaired = {**repaired, "derived_from": ["C-922"], "contradicts": ["C-879"]}
    batch = build(tmp_path, known=known).hypothesis(
        GenerateOutput.model_validate(
            {"hypothesis": {"statement": "s", "mechanism": "m", "author_confidence": 3, "claims": [repaired]}}
        )
    )
    [gap] = [r for r in batch.records if isinstance(r, Ignorance)]
    types = {r.type for r in batch.records if isinstance(r, Relation)}
    [hyp] = [r for r in batch.records if isinstance(r, Hypothesis)]
    assert types == {"part_of", "derived_from", "questions"} and "contradicts" not in types
    assert gap.hypothesis_id == hyp.id and FLAGGED in gap.content


def test_a_derivation_onto_a_record_the_store_does_not_hold_is_skipped_with_its_reason(tmp_path):
    """`derived_from` runs between claims. A relation onto a record the store does not hold would be refused
    at commit and take the whole batch with it, so the element is dropped and the reason recorded."""
    repaired, _ = internal("C-232, C-9999")
    batch = build(tmp_path, known={"C-232": claim(id="C-232")}).literature(
        LiteratureOutput.model_validate({"claims": [repaired]})
    )
    targets = [r.target_id for r in batch.records if isinstance(r, Relation)]
    assert targets == ["C-232"]
    assert batch.skipped == ["derived_from C-1 -> C-9999: C-9999 is not a claim on record"]


# --- a `reasoning` with nothing to carry it ------------------------------------------------------


# An agent fills `reasoning` on a claim that derives from nothing: the validator is correct to refuse the
# field, and the repair drops the sentence rather than the claim.
def test_a_reasoning_with_nothing_to_carry_it_costs_the_sentence_not_the_claim():
    repaired, notes = internal("doi:10.1000/michaelis", reasoning="Supported by the cytokine panel of the same cohort.")
    assert repaired["reasoning"] is None
    assert repaired["content"] == "A mechanistic step." and repaired["source"] == "doi:10.1000/michaelis"
    assert "has no derived_from or contradicts to carry it" in notes[0]


def test_a_claim_with_two_faults_is_repaired_twice_instead_of_dropped_for_the_second():
    """The repairs are applied in order and validated once, so a claim the first repair leaves invalid
    because of a second fault is not dropped."""
    repaired, notes = internal("Web search results", reasoning="Because the panel says so.")
    assert repaired["source"] is None and repaired["reasoning"] is None
    assert len(notes) == 2
    assert any("has no derived_from" in n for n in notes) and any("does not declare its kind" in n for n in notes)


def test_a_reasoning_that_the_routing_gives_a_subject_is_kept(tmp_path):
    """The order matters: when the source names claims, the routing fills `derived_from` first, so the sentence
    has somewhere to live and is not stripped by the repair that follows."""
    repaired, _ = internal("C-232, C-505; the proteostasis argument carries this step")
    assert repaired["derived_from"] == ["C-232", "C-505"]
    assert repaired["reasoning"] == "C-232, C-505; the proteostasis argument carries this step"


# --- a source that leads nowhere is reported by the phase that wrote it (bug observatory 11) ------


def test_a_claim_whose_source_names_no_published_identifier_is_reported_not_dropped():
    """A real claim's source (`C-87`): a news URL is a well-formed `url:` source, so it passes the shape test and is
    kept as literature. `canonicalise_refs` already knows it names no published identifier; that second value
    used to be discarded here and recomputed by `synthesis.gate` twelve phases later."""
    builder = build(None)
    cid = builder.claim(
        ProposedClaim(
            ref="c1",
            type="observational",
            content="A finding.",
            source="https://www.news.uzh.ch/en/articles/2025/als.html",
            quote="a verbatim sentence",
        ),
        "literature",
    )
    (record,) = [r for r in builder.batch.records if isinstance(r, Claim)]
    # Kept, exactly as written, and still literature: the claim must stay visible, not be hidden.
    assert record.source_refs == ["https://www.news.uzh.ch/en/articles/2025/als.html"]
    assert record.origin == "literature"
    assert builder.batch.unresolved == [
        {"claim_id": cid, "ref": "c1", "unresolved_source_refs": ["https://www.news.uzh.ch/en/articles/2025/als.html"]}
    ]
    assert builder.batch.skipped == []


def test_a_resolvable_source_is_not_reported_as_unresolved():
    builder = build(None)
    builder.claim(
        ProposedClaim(
            ref="c1",
            type="observational",
            content="A finding.",
            source="doi:10.1038/s41586-025-09588-6",
            quote="a verbatim sentence",
        ),
        "literature",
    )
    assert builder.batch.unresolved == []
