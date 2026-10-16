"""Deterministic retrieval of KB slices."""

from __future__ import annotations

import pytest
from kb_factories import (
    CAUSAL,
    NOW,
    claim,
    decision,
    entity,
    experiment,
    hypothesis,
    ignorance,
    intervention,
    relation,
)

from knowledge_base.retrieve import SEARCHABLE_TYPES, as_context, by_id, keyword, neighbourhood
from knowledge_base.schemas import Event
from knowledge_base.store import KnowledgeBase, read_records


def kb(*records):
    return {r.id: r for r in records}


RECORDS = kb(
    entity(id="N-1", canonical_name="C9orf72 expansion", aliases=["C9orf72 HRE"]),
    entity(id="N-2", canonical_name="DPR production"),
    hypothesis(id="H-1", statement="Autoreactive CD4 T cells drive C9-ALS", mechanism="loss of tolerance"),
    claim(id="C-1", content="C9orf72-specific CD4 T-cell responses are increased in ALS."),
    claim(id="C-2", content="IL-10 responses associate with longer predicted survival."),
    claim(id="C-3", type="causal", content="C9orf72 expansion drives DPR production", **CAUSAL),
    claim(id="C-4", content="Unrelated finding about SOD1."),
    relation(id="L-1", type="part_of", source_id="C-1", target_id="H-1"),
    relation(id="L-2", type="supports", source_id="C-2", target_id="C-1"),
    ignorance(id="I-1", content="Are the responses causal or a consequence?", hypothesis_id="H-1"),
    Event(id="V-1", event="written", record_id="C-1", run_id="RUN-1", created_at=NOW),
)


def test_by_id_returns_the_records_and_hides_events():
    assert list(by_id(["C-2", "H-1"], RECORDS)) == ["H-1", "C-2"]
    assert by_id(["V-1"], RECORDS) == {}
    with pytest.raises(KeyError, match="C-99"):
        by_id(["C-99"], RECORDS)


def test_one_hop_crosses_one_relation_with_its_far_endpoint():
    assert set(neighbourhood(["H-1"], RECORDS, hops=1)) == {"H-1", "L-1", "C-1", "I-1"}


def test_two_hops_reach_the_evidence_of_the_hypothesis_claims():
    assert set(neighbourhood(["H-1"], RECORDS, hops=2)) == {"H-1", "L-1", "C-1", "I-1", "L-2", "C-2"}


def test_claims_reach_their_entities():
    assert set(neighbourhood(["C-3"], RECORDS, hops=1)) == {"C-3", "N-1", "N-2"}


def test_zero_hops_is_the_seeds():
    assert set(neighbourhood(["C-4"], RECORDS, hops=0)) == {"C-4"}


def test_keyword_needs_every_word_and_ranks_by_occurrences():
    assert list(keyword("c9orf72", RECORDS)) == ["N-1", "C-1", "C-3"]  # N-1 names it twice
    assert list(keyword("CD4 responses", RECORDS)) == ["C-1"]
    assert keyword("tau", RECORDS) == {}
    assert keyword("  ", RECORDS) == {}
    assert len(keyword("c9orf72", RECORDS, limit=1)) == 1


# One word, "autophagy", in a record of every searchable type.
EVERY_TYPE = kb(
    claim(id="C-1", content="Autophagy failure reduces FoxP3 stability in regulatory T cells."),
    entity(id="N-1", canonical_name="autophagy"),
    hypothesis(id="H-1", statement="Autophagy failure drives Treg instability", mechanism="FoxP3 loss"),
    ignorance(id="I-1", content="Does autophagy failure precede FoxP3 loss?", hypothesis_id="H-1"),
    experiment(id="EXP-1", title="Autophagy blockade in sorted Tregs", hypothesis_id="H-1"),
    decision(id="D-1", rationale="autophagy is the decisive step", hypothesis_id="H-1", experiment_ids=["EXP-1"]),
    intervention(id="INT-1", name="Autophagy inducer", hypothesis_id="H-1"),
)


def test_keyword_searches_the_evidence_by_default():
    """An untyped search would answer the agents mostly with the system's own output (Experiments,
    Hypotheses, Ignorance records, Interventions, Decisions) rather than literature claims."""
    assert set(keyword("autophagy", EVERY_TYPE)) == {"C-1", "N-1"}
    # A claims-and-entities query is unchanged by the default, ranking included.
    assert list(keyword("c9orf72", RECORDS)) == list(keyword("c9orf72", RECORDS, types=SEARCHABLE_TYPES))


def test_keyword_reaches_the_other_record_types_when_asked():
    assert set(keyword("autophagy", EVERY_TYPE, types=["Hypothesis", "Experiment"])) == {"H-1", "EXP-1"}
    assert set(keyword("autophagy", EVERY_TYPE, types=SEARCHABLE_TYPES)) == set(EVERY_TYPE)
    assert set(keyword("autophagy", EVERY_TYPE, types=["entity"])) == {"N-1"}  # names are case-insensitive
    assert set(keyword("autophagy", EVERY_TYPE, types="Hypothesis")) == {"H-1"}  # one name, not a list


def test_an_unknown_type_name_raises_instead_of_matching_nothing():
    """`types` crosses the MCP boundary as JSON: a mistyped name must not read as an empty knowledge base."""
    with pytest.raises(ValueError, match=r"unknown record types \['Claims'\]"):
        keyword("autophagy", EVERY_TYPE, types=["Claim", "Claims"])
    with pytest.raises(ValueError, match="at least one record type"):
        keyword("autophagy", EVERY_TYPE, types=[])
    # Relations have no searchable text, so they are not a type one may ask for.
    with pytest.raises(ValueError, match="unknown record types"):
        keyword("autophagy", EVERY_TYPE, types=["Relation"])


def test_as_context_is_compact_and_carries_computed_status():
    context = as_context(by_id(["C-1", "L-2"], RECORDS), RECORDS)
    c1 = next(item for item in context if item["id"] == "C-1")
    assert c1["epistemic_status"] == "supported" and c1["superseded"] is False
    assert not {"canonical_hash", "created_at", "run_id"} & set(c1)
    assert next(item for item in context if item["id"] == "L-2")["type"] == "supports"


def test_retrieval_works_on_a_stored_kb(tmp_path):
    with KnowledgeBase.open(tmp_path, clock=lambda: NOW) as store:
        store.commit([r for r in RECORDS.values() if not isinstance(r, Event)], run_id="RUN-1")
    stored = read_records(tmp_path)
    assert set(neighbourhood(["H-1"], stored, hops=1)) == {"H-1", "L-1", "C-1", "I-1"}


def test_a_served_claim_names_the_claims_it_was_narrowed_to():
    """A PI fetched C-19 and C-95 together and nothing linked them; it cited C-19 for the clause
    the verifier had removed. Only a narrowed original carries the field."""
    from kb_factories import claim, relation

    quote = "Responses to the C9orf72 pool were selectively increased in ALS."
    records = {
        r.id: r
        for r in (
            claim(id="C-19", content="Increased for C9orf72, absent for TDP-43.", quote=quote),
            claim(id="C-95", content="Selectively increased for C9orf72.", quote=quote),
            relation(id="L-248", type="derived_from", source_id="C-95", target_id="C-19"),
        )
    }
    served = {item["id"]: item for item in as_context(records, records)}
    assert served["C-19"]["narrowed_to"] == ["C-95"]
    assert "narrowed_to" not in served["C-95"]
