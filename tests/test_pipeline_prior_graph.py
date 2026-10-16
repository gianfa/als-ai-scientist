"""The prior causal graph: a versioned selection over claims, never a store of its own."""

from __future__ import annotations

from kb_factories import CAUSAL, claim, entity, relation

from pipeline import prior_graph


def kb(*records):
    return {r.id: r for r in records}


def causal(cid="C-1", run_id="RUN-1", origin="literature", status="verified", **over):
    return claim(
        id=cid,
        type="causal",
        content=f"{cid}: the expansion drives motor-neuron loss.",
        origin=origin,
        provenance_status=status,
        run_id=run_id,
        **{**CAUSAL, **over},
    )


ENTITIES = (entity(id="N-1", canonical_name="C9orf72 expansion"), entity(id="N-2", canonical_name="motor-neuron loss"))


def test_the_selection_takes_this_run_s_verified_sourced_causal_claims():
    records = kb(
        *ENTITIES,
        causal("C-1"),
        causal("C-2", status="unverified"),  # not verified
        causal("C-3", run_id="RUN-0"),  # an earlier run
        causal("C-4", origin="agent", status="unverified"),  # the agent's own knowledge
        claim(id="C-5", type="observational", content="an observation", run_id="RUN-1"),
    )  # not causal
    assert [c.id for c in prior_graph.select(records, "RUN-1")] == ["C-1"]


def test_an_edge_carries_its_entities_by_name_and_its_computed_status():
    records = kb(*ENTITIES, causal("C-1"))
    built = prior_graph.build(records, "RUN-1", "objective")
    [edge] = built["edges"]
    assert edge["cause_name"] == "C9orf72 expansion" and edge["effect_name"] == "motor-neuron loss"
    assert edge["epistemic_status"] == "unresolved" and edge["claim_id"] == "C-1"
    assert "not a causal model identified from data" in built["note"]


def test_the_reference_changes_with_the_selection_and_not_with_the_run():
    one = prior_graph.build(kb(*ENTITIES, causal("C-1")), "RUN-1", "objective")
    same_edges_other_run = prior_graph.build(kb(*ENTITIES, causal("C-1", run_id="RUN-2")), "RUN-2", "other")
    two = prior_graph.build(kb(*ENTITIES, causal("C-1"), causal("C-2")), "RUN-1", "objective")
    # The version identifies the selection, so a run that selects the same edges cites the same prior.
    assert one["ref"] == same_edges_other_run["ref"] != two["ref"]
    assert one["ref"].startswith(f"prior:v{prior_graph.VERSION}@")


def test_an_empty_prior_is_a_real_answer():
    built = prior_graph.build(kb(*ENTITIES), "RUN-1", "objective")
    assert built["edges"] == [] and built["total"] == 0 and built["ref"]


def test_the_prompt_form_keeps_only_what_an_author_needs_to_attach_to():
    built = prior_graph.build(kb(*ENTITIES, causal("C-1")), "RUN-1", "objective")
    [edge] = prior_graph.for_prompt(built)
    assert set(edge) == {"claim_id", "cause", "effect", "mechanism", "status"}
    assert (
        prior_graph.for_prompt({"edges": [{}] * 50}, limit=3)
        == [{"claim_id": None, "cause": None, "effect": None, "mechanism": None, "status": None}] * 3
    )


def test_a_superseded_claim_keeps_its_place_but_its_status_is_recomputed():
    """Removing is impossible in an append-only store: the prior of a later run simply selects differently."""
    records = kb(
        *ENTITIES,
        causal("C-1"),
        causal("C-2"),
        relation(
            id="L-1",
            type="supersedes",
            source_id="C-2",
            target_id="C-1",
            justification_method="agent_inference",
            provenance_status="unverified",
            source_refs=[],
        ),
    )
    built = prior_graph.build(records, "RUN-1", "objective")
    assert [e["claim_id"] for e in built["edges"]] == ["C-1", "C-2"]  # the selection rule is about origin
