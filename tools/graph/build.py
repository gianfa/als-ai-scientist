"""Graphs built from the knowledge base: the one builder for the phase pipeline.

Two graph builders exist, each for its own job:

- `knowledge_base/status.py::mechanism_graph` — the causal edge list, a property of the knowledge base and the
  thing an agent reads through `mcp__kb__kb_mechanism_graph`. It stays where it is: this module calls it rather
  than reimplementing it.
- this module, which builds the whole knowledge-base graph: every record kind as a node, every relation as an
  edge, plus the structural links a relation does not carry (a claim to its entities, an experiment to its
  hypothesis, a decision to what it buys).

What it is not: a causal model. An edge here is an assertion someone made and, where a status is shown, one that
verified relations happen to support. Nothing is identified from data.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from knowledge_base.retrieve import neighbourhood
from knowledge_base.schemas import (
    Claim,
    Decision,
    Entity,
    Experiment,
    Hypothesis,
    Ignorance,
    Intervention,
    Relation,
)
from knowledge_base.status import statuses, superseded
from knowledge_base.validate import Record

KIND_OF: dict[type, str] = {
    Entity: "entity",
    Claim: "claim",
    Hypothesis: "hypothesis",
    Ignorance: "ignorance",
    Experiment: "experiment",
    Intervention: "intervention",
    Decision: "decision",
}


def _label(record: Record) -> str:
    if isinstance(record, Entity):
        return record.canonical_name
    if isinstance(record, Claim):
        return record.content
    if isinstance(record, Hypothesis):
        return record.statement
    if isinstance(record, Ignorance):
        return record.content
    if isinstance(record, Experiment):
        return record.title
    if isinstance(record, Intervention):
        return record.name
    if isinstance(record, Decision):
        return f"{record.decision} ({record.value_of_information})"
    return record.id


def _node(record: Record, status: Mapping[str, str], old: set[str]) -> dict[str, Any]:
    node: dict[str, Any] = {
        "id": record.id,
        "kind": KIND_OF[type(record)],
        "label": _label(record),
        "run_id": record.run_id,
        "superseded": record.id in old,
    }
    if isinstance(record, Claim):
        node |= {
            "type": record.type,
            "origin": record.origin,
            "provenance_status": record.provenance_status,
            "epistemic_status": status.get(record.id),
        }
    if isinstance(record, Experiment):
        node |= {"experiment_kind": record.kind, "cost": record.cost, "feasibility": record.feasibility}
    if isinstance(record, Intervention):
        node |= {"modality": record.modality, "maturity": record.maturity}
    return node


def _structural_edges(record: Record) -> list[tuple[str, str, str]]:
    """Links a record carries in its own fields, which no `Relation` record represents."""
    if isinstance(record, Claim):
        edges = [(record.id, e, "mentions") for e in record.entity_refs]
        if record.cause_ref and record.effect_ref:
            edges = [(record.cause_ref, record.id, "cause"), (record.id, record.effect_ref, "effect")] + [
                e for e in edges if e[1] not in (record.cause_ref, record.effect_ref)
            ]
        return edges
    if isinstance(record, Ignorance) and record.hypothesis_id:
        return [(record.id, record.hypothesis_id, "about")]
    if isinstance(record, Experiment):
        return [(record.id, record.hypothesis_id, "about")]
    if isinstance(record, Intervention):
        return [(record.id, record.hypothesis_id, "about")]
    if isinstance(record, Decision):
        return [(record.id, record.hypothesis_id, "about")] + [(record.id, e, "buys") for e in record.experiment_ids]
    return []


def build(
    records: Mapping[str, Record],
    run_id: str | None = None,
    focus: Iterable[str] | None = None,
    hops: int = 1,
    include_superseded: bool = False,
) -> dict[str, Any]:
    """Nodes and edges of the knowledge base, optionally narrowed.

    `run_id` keeps the records of one run, `focus` plus `hops` keeps a neighbourhood (the same traversal the
    agents' `kb_neighbourhood` uses, so a picture matches what an agent was shown). An edge whose other end was
    filtered out is dropped, and the count of those is reported: a graph that silently loses edges would mislead
    exactly where a reader looks for structure.
    """
    old = superseded(records)
    status = statuses(records)
    selected = dict(records)
    if focus:
        selected = dict(neighbourhood(list(focus), records, hops))
    if run_id:
        selected = {rid: r for rid, r in selected.items() if r.run_id == run_id}
    if not include_superseded:
        selected = {rid: r for rid, r in selected.items() if rid not in old}

    nodes = [_node(r, status, old) for r in selected.values() if type(r) in KIND_OF]
    present = {n["id"] for n in nodes}
    edges: list[dict[str, Any]] = []
    dropped = 0
    for record in selected.values():
        if isinstance(record, Relation):
            pairs = [(record.source_id, record.target_id, record.type)]
            extra = {"relation_id": record.id, "provenance_status": record.provenance_status}
        else:
            pairs, extra = _structural_edges(record), {}
        for source, target, kind in pairs:
            if source in present and target in present:
                edges.append({"source": source, "target": target, "kind": kind, **extra})
            else:
                dropped += 1
    return {
        "nodes": nodes,
        "edges": edges,
        "dropped_edges": dropped,
        "dropped_reason": (
            "an edge whose other end is outside the selection: with --run, every link to a "
            "record of an earlier run; with --focus, every link leaving the neighbourhood"
            if dropped
            else ""
        ),
        "filters": {
            "run_id": run_id,
            "focus": list(focus) if focus else None,
            "hops": hops if focus else None,
            "include_superseded": include_superseded,
        },
        "note": "Asserted records and their links, not a causal model identified from data.",
    }


__all__ = ["KIND_OF", "build"]
