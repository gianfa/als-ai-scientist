"""Computed epistemic status and the mechanism graph."""

from __future__ import annotations

from pathlib import Path

import pytest
from kb_factories import CAUSAL, claim, entity, hypothesis, ignorance, relation

from knowledge_base.status import (
    causal_edges,
    epistemic_status,
    is_active,
    is_improper,
    is_proper,
    mechanism_graph,
    mechanism_path,
    mechanism_subgraph,
    narrowings,
    proper_mechanism,
    statuses,
)

UNVERIFIED = {"origin": "agent", "provenance_status": "unverified", "verification_method": "none", "source_refs": []}


def kb(*records):
    return {r.id: r for r in records}


TARGET = claim(id="C-1", content="Target finding.")
EVIDENCE = claim(id="C-2", content="Verified evidence.")
OTHER = claim(id="C-4", content="More verified evidence.")
GUESS = claim(id="C-3", content="An agent's guess.", **UNVERIFIED)


def supports(rid, source, **kw):
    return relation(id=rid, type="supports", source_id=source, target_id="C-1", **kw)


def contradicts(rid, source, **kw):
    return relation(id=rid, type="contradicts", source_id=source, target_id="C-1", **kw)


def test_no_relation_means_unresolved():
    assert epistemic_status("C-1", kb(TARGET)) == "unresolved"


@pytest.mark.parametrize(
    "relations, expected",
    [
        ([supports("L-1", "C-2")], "supported"),
        ([contradicts("L-1", "C-2")], "contradicted"),
        ([supports("L-1", "C-2"), contradicts("L-2", "C-4")], "contested"),
    ],
)
def test_status_from_active_relations(relations, expected):
    assert epistemic_status("C-1", kb(TARGET, EVIDENCE, OTHER, *relations)) == expected


def test_relation_from_an_unverified_claim_carries_no_weight():
    records = kb(TARGET, GUESS, supports("L-1", "C-3"))
    assert not is_active(records["L-1"], records)
    assert epistemic_status("C-1", records) == "unresolved"


def test_unverified_inference_from_a_verified_claim_carries_no_weight():
    records = kb(
        TARGET, EVIDENCE, supports("L-1", "C-2", justification_method="agent_inference", provenance_status="unverified")
    )
    assert epistemic_status("C-1", records) == "unresolved"


def test_questions_is_never_negative_evidence():
    records = kb(
        TARGET,
        hypothesis(id="H-1"),
        ignorance(id="I-1"),
        relation(
            id="L-1",
            type="questions",
            source_id="I-1",
            target_id="C-1",
            justification_method="agent_inference",
            provenance_status="unverified",
            source_refs=[],
        ),
    )
    assert epistemic_status("C-1", records) == "unresolved"


def test_status_is_for_claims_only():
    records = kb(EVIDENCE, hypothesis(id="H-1"))
    assert statuses(records) == {"C-2": "unresolved"}
    with pytest.raises(KeyError):
        epistemic_status("H-1", records)


def test_a_superseded_claim_no_longer_gives_evidence():
    new = claim(id="C-6", content="Corrected evidence.")
    records = kb(
        TARGET,
        EVIDENCE,
        new,
        supports("L-1", "C-2"),
        relation(
            id="L-2",
            type="supersedes",
            source_id="C-6",
            target_id="C-2",
            justification_method="agent_inference",
            provenance_status="unverified",
            source_refs=[],
        ),
    )
    assert epistemic_status("C-1", records) == "unresolved"


def test_evidence_on_a_superseded_claim_is_not_transferred():
    new = claim(id="C-6", content="Revised target.")
    records = kb(
        TARGET,
        EVIDENCE,
        new,
        supports("L-1", "C-2"),
        relation(
            id="L-2",
            type="supersedes",
            source_id="C-6",
            target_id="C-1",
            justification_method="agent_inference",
            provenance_status="unverified",
            source_refs=[],
        ),
    )
    assert epistemic_status("C-1", records) == "supported"
    assert epistemic_status("C-6", records) == "unresolved"


def test_mechanism_graph_has_one_edge_per_causal_claim_with_its_status():
    causal = claim(id="C-5", type="causal", content="C9orf72 expansion drives DPR production", **CAUSAL)
    records = kb(
        entity(id="N-1", canonical_name="C9orf72 expansion"),
        entity(id="N-2", canonical_name="DPR production"),
        causal,
        EVIDENCE,
        TARGET,
        relation(id="L-1", type="supports", source_id="C-2", target_id="C-5"),
    )
    [edge] = mechanism_graph(records)
    assert (edge.claim_id, edge.cause_name, edge.effect_name) == ("C-5", "C9orf72 expansion", "DPR production")
    assert not hasattr(edge, "sign")  # ADR 0032
    assert (edge.provenance_status, edge.epistemic_status) == ("verified", "supported")


def test_mechanism_graph_hides_superseded_causal_claims_unless_asked():
    old = claim(id="C-5", type="causal", content="C9orf72 expansion drives DPR production", **CAUSAL)
    new = claim(id="C-6", type="causal", content="C9orf72 expansion drives DPR production in neurons", **CAUSAL)
    records = kb(
        entity(id="N-1"),
        entity(id="N-2", canonical_name="DPR production"),
        old,
        new,
        relation(
            id="L-1",
            type="supersedes",
            source_id="C-6",
            target_id="C-5",
            justification_method="agent_inference",
            provenance_status="unverified",
            source_refs=[],
        ),
    )
    assert [e.claim_id for e in mechanism_graph(records)] == ["C-6"]
    assert [e.claim_id for e in mechanism_graph(records, include_superseded=True)] == ["C-5", "C-6"]


# --- filtering the mechanism graph, and reading a path out of it -------------------------------------------

UNVERIFIED_CAUSAL = {**UNVERIFIED, "type": "causal"}


def edge(claim_id: str, cause: str, effect: str, **kw):
    """One causal claim from `cause` to `effect`, unverified unless `kw` says otherwise."""
    fields = {**UNVERIFIED_CAUSAL, **CAUSAL, "cause_ref": cause, "effect_ref": effect}
    fields.update(kw)
    return claim(id=claim_id, content=f"{cause} causes {effect}", **fields)


def chain(*names: str, verified: str | None = None):
    """A knowledge base holding entities `names` and the edges joining them in order."""
    entities = [entity(id=n, canonical_name=f"name of {n}") for n in names]
    edges = [
        edge(
            f"C-{i + 10}",
            a,
            b,
            **(
                {}
                if f"C-{i + 10}" != verified
                else {
                    "provenance_status": "verified",
                    "verification_method": "quote",
                    "origin": "literature",
                    "source_refs": ["doi:10.1038/x"],
                }
            ),
        )
        for i, (a, b) in enumerate(zip(names, names[1:]))
    ]
    return kb(*entities, *edges)


def test_causal_edges_filters_on_provenance_and_on_epistemic_standing():
    records = chain("N-1", "N-2", "N-3", verified="C-11")
    assert [e.claim_id for e in causal_edges(records)] == ["C-10", "C-11"]
    assert [e.claim_id for e in causal_edges(records, provenance=["verified"])] == ["C-11"]
    # Nothing points at either claim, so both are unresolved and neither is supported.
    assert [e.claim_id for e in causal_edges(records, epistemic=["unresolved"])] == ["C-10", "C-11"]
    assert causal_edges(records, epistemic=["supported"]) == []


def test_an_empty_filter_selects_nothing_rather_than_everything():
    records = chain("N-1", "N-2")
    assert causal_edges(records, provenance=[]) == []
    assert causal_edges(records, epistemic=[]) == []


def test_aliases_relabel_the_edges_and_take_the_surviving_name():
    # Two separate assertions that join only once N-3 and N-2 are known to be one node.
    records = kb(
        entity(id="N-1"),
        entity(id="N-2", canonical_name="the effect"),
        entity(id="N-3", canonical_name="the effect, spelled otherwise"),
        entity(id="N-4", canonical_name="the far end"),
        edge("C-10", "N-1", "N-2"),
        edge("C-11", "N-3", "N-4"),
    )
    assert mechanism_path(records, require_verified=False) == []
    joined = mechanism_path(records, aliases={"N-3": "N-2"}, require_verified=False)
    assert [(e.claim_id, e.cause_name, e.effect_name) for e in joined] == [
        ("C-10", "C9orf72 expansion", "the effect"),
        ("C-11", "the effect", "the far end"),
    ]


def test_an_edge_whose_ends_merge_is_not_a_self_loop_in_the_path():
    records = kb(
        entity(id="N-1"),
        entity(id="N-2", canonical_name="b"),
        entity(id="N-3", canonical_name="c"),
        edge("C-10", "N-1", "N-2"),
        edge("C-11", "N-2", "N-3"),
    )
    # Merging the two ends of C-11 would assert that a thing causes itself; the edge is dropped instead.
    assert [e.claim_id for e in causal_edges(records, aliases={"N-3": "N-2"})] == ["C-10"]


def test_mechanism_path_returns_the_longest_chain_in_order():
    path = mechanism_path(chain("N-1", "N-2", "N-3", "N-4"), require_verified=False)
    assert [e.claim_id for e in path] == ["C-10", "C-11", "C-12"]
    assert [(e.cause_id, e.effect_id) for e in path] == [("N-1", "N-2"), ("N-2", "N-3"), ("N-3", "N-4")]


def test_a_set_of_disjoint_edges_is_no_path():
    records = kb(
        entity(id="N-1"),
        entity(id="N-2", canonical_name="b"),
        entity(id="N-3", canonical_name="c"),
        entity(id="N-4", canonical_name="d"),
        edge("C-10", "N-1", "N-2"),
        edge("C-11", "N-3", "N-4"),
    )
    assert mechanism_path(records, require_verified=False) == []


def test_a_verified_step_is_preferred_to_a_longer_unchecked_chain():
    # Two separate components: a two-edge chain with a verified step, and a three-edge chain with none.
    records = kb(
        *(entity(id=f"N-{i}", canonical_name=f"name of N-{i}") for i in range(1, 8)),
        edge(
            "C-10",
            "N-1",
            "N-2",
            provenance_status="verified",
            verification_method="quote",
            origin="literature",
            source_refs=["doi:10.1038/x"],
        ),
        edge("C-11", "N-2", "N-3"),
        edge("C-20", "N-4", "N-5"),
        edge("C-21", "N-5", "N-6"),
        edge("C-22", "N-6", "N-7"),
    )
    assert [e.claim_id for e in mechanism_path(records)] == ["C-10", "C-11"]
    # Asked for the longest chain regardless, the unchecked one wins.
    assert [e.claim_id for e in mechanism_path(records, require_verified=False)] == ["C-20", "C-21", "C-22"]


def test_an_entirely_unchecked_graph_yields_no_path_when_a_verified_step_is_required():
    records = chain("N-1", "N-2", "N-3")
    assert mechanism_path(records) == []
    assert len(mechanism_path(records, require_verified=False)) == 2


def test_a_cycle_does_not_produce_an_unbounded_walk():
    records = chain("N-1", "N-2", "N-3")
    records["C-99"] = edge("C-99", "N-3", "N-1")
    # A simple path visits N-1 once, so the closing edge is unusable: three nodes, two edges, no walk.
    path = mechanism_path(records, require_verified=False)
    assert len(path) == 2
    assert len({e.cause_id for e in path} | {e.effect_id for e in path}) == 3


def test_min_edges_is_the_floor_below_which_there_is_no_chain():
    records = chain("N-1", "N-2")
    assert mechanism_path(records, require_verified=False) == []  # the default floor, 2, is the smallest join
    assert len(mechanism_path(records, require_verified=False, min_edges=1)) == 1


# --- the backbone plus its one-hop neighbourhood -----------------------------------------------------------

VERIFIED_EDGE = {
    "provenance_status": "verified",
    "verification_method": "quote",
    "origin": "literature",
    "source_refs": ["doi:10.1038/x"],
}


def spine_with_neighbours():
    """A three-node backbone (C-10 verified, C-11 not) with four adjacent edges around it."""
    return kb(
        *(entity(id=f"N-{i}", canonical_name=f"name of N-{i}") for i in range(1, 9)),
        edge("C-10", "N-1", "N-2", **VERIFIED_EDGE),
        edge("C-11", "N-2", "N-3"),
        edge("C-20", "N-4", "N-2"),  # into a backbone node, unverified
        edge("C-21", "N-2", "N-5"),  # out of one, unverified
        # Into the tail, so it is adjacent without extending the chain — the point is its provenance.
        edge("C-22", "N-6", "N-3", **VERIFIED_EDGE),
        edge("C-23", "N-7", "N-8"),
    )  # not adjacent at all


def test_the_subgraph_keeps_the_path_as_its_backbone():
    sub = mechanism_subgraph(spine_with_neighbours())
    assert [e.claim_id for e in sub.backbone] == ["C-10", "C-11"]


def test_only_hypothesised_neighbours_of_a_backbone_node_are_kept_by_default():
    sub = mechanism_subgraph(spine_with_neighbours())
    # C-22 is adjacent but verified, C-23 is unverified but not adjacent: neither is a hypothesised neighbourhood.
    assert [e.claim_id for e in sub.neighbourhood] == ["C-20", "C-21"]


def test_the_neighbour_filter_is_a_parameter_on_both_axes():
    records = spine_with_neighbours()
    verified_only = mechanism_subgraph(records, neighbour_provenance=["verified"])
    assert [e.claim_id for e in verified_only.neighbourhood] == ["C-22"]
    assert [e.claim_id for e in mechanism_subgraph(records, neighbour_provenance=None).neighbourhood] == [
        "C-20",
        "C-21",
        "C-22",
    ]
    assert mechanism_subgraph(records, neighbour_provenance=[]).neighbourhood == []
    # Nothing points at any of these claims, so every one of them is unresolved.
    assert mechanism_subgraph(records, neighbour_epistemic=["supported"]).neighbourhood == []


def test_a_backbone_step_is_never_also_a_fringe_edge():
    sub = mechanism_subgraph(spine_with_neighbours(), neighbour_provenance=None)
    assert {e.claim_id for e in sub.backbone} & {e.claim_id for e in sub.neighbourhood} == set()
    assert len(sub.edges) == len({e.claim_id for e in sub.edges})


def test_an_untaken_edge_between_two_backbone_nodes_is_fringe():
    records = spine_with_neighbours()
    records["C-30"] = edge("C-30", "N-1", "N-3")  # a shortcut the path did not take
    sub = mechanism_subgraph(records)
    assert [e.claim_id for e in sub.backbone] == ["C-10", "C-11"]
    assert "C-30" in [e.claim_id for e in sub.neighbourhood]


def test_no_path_means_no_subgraph_rather_than_a_bare_neighbourhood():
    sub = mechanism_subgraph(chain("N-1", "N-2", "N-3"))  # nothing verified, so no admissible path
    assert (sub.backbone, sub.neighbourhood) == ([], [])


def test_node_ids_lists_the_spine_in_path_order_then_the_outer_ends():
    sub = mechanism_subgraph(spine_with_neighbours())
    assert sub.node_ids == ["N-1", "N-2", "N-3", "N-4", "N-5"]


QUOTE = "Responses to the C9orf72 pool were selectively increased in ALS (P = 0.001)."


def test_a_narrowing_is_a_verified_derived_from_between_claims_sharing_one_quote():
    """`Builder.narrowed` keeps the original's quote on the restatement: that is what tells it from an agent's
    own `derived_from`, which joins claims with different quotes or none (seen in a real run: C-95 from C-19)."""
    original = claim(id="C-19", content="Increased for C9orf72, absent for TDP-43 and SOD1.", quote=QUOTE)
    narrowed = claim(id="C-95", content="Selectively increased for C9orf72 (P = 0.001).", quote=QUOTE)
    other = claim(id="C-7", content="An agent's derivation.", quote="another sentence")
    records = kb(
        original,
        narrowed,
        other,
        relation(id="L-248", type="derived_from", source_id="C-95", target_id="C-19"),
        relation(id="L-9", type="derived_from", source_id="C-7", target_id="C-19"),
    )
    assert narrowings(records) == {"C-19": ["C-95"]}
    unverified = records | {
        "L-248": relation(
            id="L-248",
            type="derived_from",
            source_id="C-95",
            target_id="C-19",
            provenance_status="unverified",
            justification_method="agent_inference",
        )
    }
    assert narrowings(unverified) == {}
    # It is a view, not a demotion: the original keeps its relations and its status.
    assert statuses(records)["C-19"] == statuses(kb(original))["C-19"]


ARCHIVE_011 = Path(__file__).resolve().parents[1] / "experiments" / "pl-prod-011"


@pytest.mark.skipif(not (ARCHIVE_011 / "kb").is_dir(), reason="the pl-prod-011 archive is not in this checkout")
def test_pl_prod_011_narrowings_are_the_ones_its_verifier_wrote():
    """Six confirmed-but-unfaithful verdicts were narrowed, on five claims; the two causal ones (C-32, C-83)
    were refused by `Builder.narrowed` and have none. Measured on the archive, read-only."""
    from knowledge_base.store import read_records

    found = narrowings(read_records(ARCHIVE_011 / "kb"))
    assert found["C-19"] == ["C-95"]
    assert sorted(found) == ["C-19", "C-22", "C-33", "C-34", "C-43"]
    assert sum(map(len, found.values())) == 6


# --- proper edges and the mechanism of ADR 0033 -----------------------------------------------------------


def support_for(rid: str, target: str, source: str = "C-90"):
    """A verified `supports` relation from a verified claim onto `target`."""
    return relation(id=rid, type="supports", source_id=source, target_id=target)


def contradiction_for(rid: str, target: str, source: str = "C-91"):
    return relation(id=rid, type="contradicts", source_id=source, target_id=target)


EVIDENCE_90 = claim(id="C-90", content="Verified evidence for an edge.")
EVIDENCE_91 = claim(id="C-91", content="Verified evidence against an edge.")


def only(records, claim_id):
    return next(e for e in mechanism_graph(records) if e.claim_id == claim_id)


def test_a_verified_edge_is_proper():
    records = chain("N-1", "N-2", verified="C-10")
    assert is_proper(only(records, "C-10")) and not is_improper(only(records, "C-10"))


def test_an_unchecked_edge_is_improper():
    records = chain("N-1", "N-2")
    assert is_improper(only(records, "C-10")) and not is_proper(only(records, "C-10"))


def test_an_unchecked_edge_supported_by_a_verified_relation_is_proper():
    records = chain("N-1", "N-2")
    records.update(kb(EVIDENCE_90, support_for("L-1", "C-10")))
    assert only(records, "C-10").provenance_status == "unverified"
    assert is_proper(only(records, "C-10"))


def test_a_contradicted_edge_is_neither_proper_nor_improper_even_when_verified():
    records = chain("N-1", "N-2", verified="C-10")
    records.update(kb(EVIDENCE_91, contradiction_for("L-1", "C-10")))
    edge_ = only(records, "C-10")
    assert edge_.epistemic_status == "contradicted"
    assert not is_proper(edge_) and not is_improper(edge_)


def test_a_contested_edge_is_hidden_from_the_view():
    records = chain("N-1", "N-2", verified="C-10")
    records.update(kb(EVIDENCE_90, EVIDENCE_91, support_for("L-1", "C-10"), contradiction_for("L-2", "C-10")))
    edge_ = only(records, "C-10")
    assert edge_.epistemic_status == "contested"
    assert not is_proper(edge_) and not is_improper(edge_)


def test_edge_filter_expresses_what_the_two_set_filters_cannot():
    # "verified OR supported" is not the AND of a provenance set and an epistemic set.
    records = kb(
        *(entity(id=f"N-{i}", canonical_name=f"name of N-{i}") for i in range(1, 6)),
        edge("C-10", "N-1", "N-2", **VERIFIED_EDGE),
        edge("C-11", "N-3", "N-4"),
        edge("C-12", "N-4", "N-5"),
        EVIDENCE_90,
        support_for("L-1", "C-11"),
    )
    assert [e.claim_id for e in causal_edges(records, edge_filter=is_proper)] == ["C-10", "C-11"]
    assert [e.claim_id for e in causal_edges(records, edge_filter=is_improper)] == ["C-12"]


def test_proper_mechanism_backbone_is_all_proper_and_its_neighbours_all_improper():
    records = kb(
        *(entity(id=f"N-{i}", canonical_name=f"name of N-{i}") for i in range(1, 8)),
        edge("C-10", "N-1", "N-2", **VERIFIED_EDGE),
        edge("C-11", "N-2", "N-3", **VERIFIED_EDGE),
        edge("C-20", "N-3", "N-4"),  # improper, continues the chain: the hypothesis step
        edge("C-21", "N-2", "N-5"),  # improper, hangs off an inner node
        edge("C-22", "N-6", "N-7"),  # improper, not adjacent
        edge("C-23", "N-5", "N-6", **VERIFIED_EDGE),  # proper but not connected to the backbone
    )
    sub = proper_mechanism(records)
    assert [e.claim_id for e in sub.backbone] == ["C-10", "C-11"]
    assert [e.claim_id for e in sub.neighbourhood] == ["C-20", "C-21"]
    assert all(is_proper(e) for e in sub.backbone) and all(is_improper(e) for e in sub.neighbourhood)


def test_proper_mechanism_returns_a_chain_of_two_whenever_the_kb_holds_one():
    records = kb(
        *(entity(id=f"N-{i}", canonical_name=f"name of N-{i}") for i in range(1, 6)),
        edge("C-10", "N-1", "N-2", **VERIFIED_EDGE),  # a lone proper edge
        edge("C-20", "N-3", "N-4", **VERIFIED_EDGE),
        edge("C-21", "N-4", "N-5", **VERIFIED_EDGE),
    )
    assert [e.claim_id for e in proper_mechanism(records).backbone] == ["C-20", "C-21"]


def test_proper_mechanism_falls_back_to_one_edge_and_to_nothing():
    one = kb(entity(id="N-1"), entity(id="N-2", canonical_name="b"), edge("C-10", "N-1", "N-2", **VERIFIED_EDGE))
    assert [e.claim_id for e in proper_mechanism(one).backbone] == ["C-10"]
    assert proper_mechanism(one, min_edges=2).backbone == []
    assert proper_mechanism(chain("N-1", "N-2", "N-3")).backbone == []  # nothing proper at all


def test_a_contradicted_edge_does_not_enter_the_backbone_or_the_neighbourhood():
    records = chain("N-1", "N-2", "N-3", verified="C-10")
    records["C-11"] = edge("C-11", "N-2", "N-3", **VERIFIED_EDGE)
    records.update(kb(EVIDENCE_91, contradiction_for("L-1", "C-11")))
    sub = proper_mechanism(records)
    assert [e.claim_id for e in sub.backbone] == ["C-10"]
    assert sub.neighbourhood == []


def test_proper_mechanism_uses_aliases_to_join_proper_edges():
    records = kb(
        entity(id="N-1"),
        entity(id="N-2", canonical_name="the effect"),
        entity(id="N-3", canonical_name="the effect, spelled otherwise"),
        entity(id="N-4", canonical_name="the far end"),
        edge("C-10", "N-1", "N-2", **VERIFIED_EDGE),
        edge("C-11", "N-3", "N-4", **VERIFIED_EDGE),
    )
    assert len(proper_mechanism(records).backbone) == 1
    assert len(proper_mechanism(records, aliases={"N-3": "N-2"}).backbone) == 2
