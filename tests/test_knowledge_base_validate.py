"""Cross-record rules of the knowledge base."""

from __future__ import annotations

import pytest
from kb_factories import CAUSAL, NOW, claim, decision, entity, experiment, hypothesis, ignorance, intervention, relation

from knowledge_base.schemas import Event
from knowledge_base.validate import ENDPOINTS, problems


def kb(*records):
    return {r.id: r for r in records}


BASE = kb(
    entity(id="N-1", canonical_name="C9orf72 expansion", aliases=["C9orf72 HRE"]),
    entity(id="N-2", canonical_name="DPR production"),
    claim(id="C-1"),
    claim(id="C-2", content="Poly-GA aggregates are found in motor cortex."),
    claim(
        id="C-3",
        content="An agent guess.",
        origin="agent",
        provenance_status="unverified",
        verification_method="none",
        source_refs=[],
    ),
    hypothesis(id="H-1"),
    ignorance(id="I-1", hypothesis_id="H-1"),
)


def test_a_valid_record_has_no_problems():
    assert problems(relation(id="L-9", source_id="C-1", target_id="C-2"), BASE) == []
    assert problems(claim(id="C-9", type="causal", content="expansion drives DPRs", **CAUSAL), BASE) == []


def test_ids_are_unique():
    assert problems(claim(id="C-1", content="new text"), BASE) == ["C-1 already exists"]


# --- endpoints -------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rtype, source, target",
    [
        ("part_of", "C-1", "H-1"),
        ("questions", "I-1", "C-1"),
        ("questions", "I-1", "H-1"),
        ("depends_on", "H-1", "C-1"),
        ("supersedes", "C-2", "C-1"),
    ],
)
def test_allowed_endpoints(rtype, source, target):
    rel = relation(id="L-9", type=rtype, source_id=source, target_id=target)
    assert problems(rel, BASE) == []


@pytest.mark.parametrize(
    "rtype, source, target, fragment",
    [
        ("questions", "C-1", "C-2", "cannot start from C-1"),
        ("part_of", "C-1", "C-2", "cannot point to C-2"),
        ("supports", "I-1", "C-1", "cannot start from I-1"),
        ("supersedes", "H-1", "C-1", "same type"),
        ("supports", "C-1", "H-1", "cannot point to H-1"),
        ("contradicts", "C-1", "H-1", "cannot point to H-1"),
    ],
)
def test_rejected_endpoints(rtype, source, target, fragment):
    rel = relation(id="L-9", type=rtype, source_id=source, target_id=target)
    assert any(fragment in p for p in problems(rel, BASE))


def test_every_relation_type_has_an_endpoint_rule():
    from typing import get_args

    from knowledge_base.schemas import RelationType

    assert set(ENDPOINTS) == set(get_args(RelationType))


def test_tests_relations_start_from_an_experiment():
    with_exp = {**BASE, "EXP-1": experiment()}
    assert problems(relation(id="L-9", type="tests", source_id="EXP-1", target_id="C-1"), with_exp) == []
    assert any(
        "cannot start from C-1" in p
        for p in problems(relation(id="L-9", type="tests", source_id="C-1", target_id="C-2"), BASE)
    )


def test_experiment_and_decision_references_must_exist_and_match():
    assert problems(experiment(), BASE) == []
    assert problems(experiment(id="EXP-2", hypothesis_id="H-9"), BASE) == ["experiment EXP-2 refers to unknown H-9"]
    with_exp = {**BASE, "EXP-1": experiment(), "H-2": hypothesis(id="H-2")}
    assert problems(decision(), with_exp) == []
    assert problems(decision(id="D-2", hypothesis_id="H-2"), with_exp) == [
        "decision D-2 buys EXP-1, which belongs to H-1"
    ]


def test_intervention_references_must_exist_and_depends_on_may_start_from_it():
    assert problems(intervention(), BASE) == []
    assert problems(intervention(id="INT-2", hypothesis_id="H-9", acts_on_claims=["C-99"]), BASE) == [
        "intervention INT-2 refers to unknown H-9",
        "intervention INT-2 refers to unknown C-99",
    ]
    with_int = {**BASE, "INT-1": intervention()}
    assert problems(relation(id="L-9", type="depends_on", source_id="INT-1", target_id="C-1"), with_int) == []
    assert any(
        "cannot" in p
        for p in problems(relation(id="L-9", type="supports", source_id="INT-1", target_id="C-1"), with_int)
    )


def test_relation_endpoints_must_exist():
    assert "relation L-9 refers to unknown C-99" in problems(relation(id="L-9", target_id="C-99"), BASE)


# --- contradicts ------------------------------------------------------------------------------------------


def test_contradicts_from_an_unverified_claim_is_rejected():
    rel = relation(
        id="L-9",
        type="contradicts",
        source_id="C-3",
        target_id="C-1",
        justification_method="agent_inference",
        provenance_status="unverified",
    )
    assert any("record it as questions" in p for p in problems(rel, BASE))


def test_contradicts_from_a_verified_claim_is_accepted():
    rel = relation(id="L-9", type="contradicts", source_id="C-2", target_id="C-1")
    assert problems(rel, BASE) == []


# --- claims and entities ----------------------------------------------------------------------------------


def test_claim_entities_must_be_registered():
    c = claim(id="C-9", type="causal", content="x drives y", **{**CAUSAL, "effect_ref": "N-77"})
    assert problems(c, BASE) == ["claim C-9 refers to unknown N-77"]


def test_claim_refs_must_point_to_entities():
    c = claim(id="C-9", content="other", entity_refs=["N-1"])
    assert problems(c, {**BASE, "N-1": hypothesis(id="H-5")}) == ["claim C-9 refers to N-1, which is a Hypothesis"]


def test_duplicate_claim_is_rejected_with_a_pointer_to_seen_again():
    copy = claim(id="C-9", content="  dprs are DETECTED in c9-als spinal cord ")
    assert any("duplicates C-1" in p and "seen_again" in p for p in problems(copy, BASE))


def test_entity_names_cannot_collide_across_entities():
    clash = entity(id="N-9", canonical_name="Repeat expansion", aliases=["c9orf72 hre"])
    assert problems(clash, BASE) == ["entity N-9 reuses the name(s) ['c9orf72 hre'] of N-1"]


# --- ignorance and events ---------------------------------------------------------------------------------


def test_ignorance_must_name_an_existing_hypothesis():
    assert problems(ignorance(id="I-9", hypothesis_id="H-77"), BASE) == ["ignorance I-9 refers to unknown H-77"]
    assert problems(ignorance(id="I-9"), BASE) == []


def test_events_point_to_existing_records_except_rejections():
    seen = Event(id="V-1", event="seen_again", record_id="C-1", run_id="RUN-2", created_at=NOW)
    assert problems(seen, BASE) == []
    ghost = Event(id="V-2", event="written", record_id="C-77", run_id="RUN-2", created_at=NOW)
    assert problems(ghost, BASE) == ["event V-2 refers to unknown C-77"]
    rejected = Event(
        id="V-3", event="rejected", record_id="C-77", detail="bad endpoint", run_id="RUN-2", created_at=NOW
    )
    assert problems(rejected, BASE) == []
