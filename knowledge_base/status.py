"""Epistemic status of claims and the mechanism graph, both computed from the records. Pure functions.

No record stores an epistemic status: it is derived here, every time, from the relations pointing at a record.

    current(C)        = no supersedes relation in the KB targets C
    active(relation)  = relation.provenance_status == verified AND source claim verified AND current(source)
    status(target)    = contested     if active supports AND active contradicts
                        supported     if active supports
                        contradicted  if active contradicts
                        unresolved    otherwise

Status is defined for claims only: a hypothesis is a composite of claims and needs its own aggregation rule, not
defined yet. A superseded claim stays in the KB but no longer gives evidence; relations pointing at it stay on it
and are not transferred to the new claim (supersedes is not logical equivalence). Relations of other types
(`questions`, `part_of`, ...) never change a status: in particular a `questions` relation is not negative evidence.

The mechanism graph is a view over current causal claims (superseded ones on request): one edge per causal claim,
cause entity -> effect entity, carrying the claim's provenance and computed status, so verified and merely
hypothesised edges are told apart.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Final, Literal

from .schemas import Claim, Entity, Relation
from .validate import Record

EpistemicStatus = Literal["contested", "supported", "contradicted", "unresolved"]
WEIGHTED: Final = ("supports", "contradicts")


def superseded(records: Mapping[str, Record]) -> set[str]:
    return {r.target_id for r in records.values() if isinstance(r, Relation) and r.type == "supersedes"}


def narrowings(records: Mapping[str, Record]) -> dict[str, list[str]]:
    """Original claim ID -> the claims the verifier narrowed it to (`pipeline.proposals.Builder.narrowed`).

    A narrowing is the one `derived_from` that joins two verified-relation claims carrying the **same** quote: the
    restatement keeps the original's quote and says only what it says. An agent's own `derived_from` joins claims
    with different quotes, or none, so it is not read as one. The original is not superseded — that would
    deactivate every relation it has, on one verifier's judgement — so this is how a reader learns it overstated
    its quote and which claim states the quote's part alone.
    """
    out: dict[str, list[str]] = {}
    for r in records.values():
        if isinstance(r, Relation) and r.type == "derived_from" and r.provenance_status == "verified":
            narrowed, original = records.get(r.source_id), records.get(r.target_id)
            if (
                isinstance(narrowed, Claim)
                and isinstance(original, Claim)
                and narrowed.quote
                and narrowed.quote == original.quote
            ):
                out.setdefault(original.id, []).append(narrowed.id)
    return out


def is_active(relation: Relation, records: Mapping[str, Record], old: set[str] | None = None) -> bool:
    source = records.get(relation.source_id)
    old = superseded(records) if old is None else old
    return (
        relation.provenance_status == "verified"
        and isinstance(source, Claim)
        and source.provenance_status == "verified"
        and source.id not in old
    )


def _status(has_support: bool, has_contradiction: bool) -> EpistemicStatus:
    if has_support and has_contradiction:
        return "contested"
    if has_support:
        return "supported"
    if has_contradiction:
        return "contradicted"
    return "unresolved"


def visible_at(records: Mapping[str, Record], cutoff: datetime) -> dict[str, Record]:
    """The records that existed at `cutoff`. The knowledge base is shared and append-only, so a status computed
    today is not the status a phase saw: a hypothesis with no contradicted claim when its run ended may have some
    on today's records. Anything asking what the system knew *then* restricts here
    first, so there is one definition of "as of" in the repository and not one per caller."""
    return {i: r for i, r in records.items() if r.created_at <= cutoff}


def statuses_as_of(records: Mapping[str, Record], cutoff: datetime) -> dict[str, EpistemicStatus]:
    """Claim statuses as they stood at `cutoff`."""
    return statuses(visible_at(records, cutoff))


def statuses(records: Mapping[str, Record]) -> dict[str, EpistemicStatus]:
    """The status of every claim in the KB (one pass over the relations)."""
    old = superseded(records)
    support: set[str] = set()
    contradiction: set[str] = set()
    for record in records.values():
        if isinstance(record, Relation) and record.type in WEIGHTED and is_active(record, records, old):
            (support if record.type == "supports" else contradiction).add(record.target_id)
    return {rid: _status(rid in support, rid in contradiction) for rid, r in records.items() if isinstance(r, Claim)}


def epistemic_status(record_id: str, records: Mapping[str, Record]) -> EpistemicStatus:
    if not isinstance(records.get(record_id), Claim):
        raise KeyError(f"{record_id} is not a claim of the KB")
    return statuses(records)[record_id]


@dataclass(frozen=True)
class MechanismEdge:
    claim_id: str
    cause_id: str
    cause_name: str
    effect_id: str
    effect_name: str
    context: str
    mechanism: str
    provenance_status: str
    epistemic_status: EpistemicStatus


# What the mechanism view leaves out (ADR 0033): the store keeps a contradicted or contested claim and its
# `contradicts` relations, but an implication that other verified claims contradict is not read as part of a mechanism.
HIDDEN_FROM_VIEW: Final = ("contradicted", "contested")


def is_visible(edge: MechanismEdge) -> bool:
    """Whether the mechanism view shows the edge: it is not contradicted and not contested (ADR 0033)."""
    return edge.epistemic_status not in HIDDEN_FROM_VIEW


def is_proper(edge: MechanismEdge) -> bool:
    """A proper edge: shown by the view, and either checked against its source (`provenance_status == "verified"`) or
    supported by an active relation (`epistemic_status == "supported"`). Anything else that is shown is improper: an
    agent's unchecked assertion. The two axes stay separate on the edge; this is the one place that combines them."""
    return is_visible(edge) and (edge.provenance_status == "verified" or edge.epistemic_status == "supported")


def is_improper(edge: MechanismEdge) -> bool:
    return is_visible(edge) and not is_proper(edge)


def mechanism_graph(records: Mapping[str, Record], include_superseded: bool = False) -> list[MechanismEdge]:
    """One edge per current causal claim (also superseded ones if asked), in record order."""
    status = statuses(records)
    old = set() if include_superseded else superseded(records)

    def name(entity_id: str) -> str:
        entity = records.get(entity_id)
        return entity.canonical_name if isinstance(entity, Entity) else entity_id

    return [
        MechanismEdge(
            claim_id=c.id,
            cause_id=c.cause_ref,
            cause_name=name(c.cause_ref),
            effect_id=c.effect_ref,
            effect_name=name(c.effect_ref),
            context=c.context,
            mechanism=c.mechanism,
            provenance_status=c.provenance_status,
            epistemic_status=status[c.id],
        )
        for c in records.values()
        if isinstance(c, Claim) and c.causal_fields() is not None and c.id not in old
    ]


def causal_edges(
    records: Mapping[str, Record],
    *,
    include_superseded: bool = False,
    provenance: Collection[str] | None = None,
    epistemic: Collection[EpistemicStatus] | None = None,
    aliases: Mapping[str, str] | None = None,
    edge_filter: Callable[[MechanismEdge], bool] | None = None,
) -> list[MechanismEdge]:
    """The mechanism graph, filtered by the epistemic standing of its edges.

    `mechanism_graph` carries `provenance_status` and `epistemic_status` on every edge but selects on neither,
    so every caller that wanted "the verified part of the mechanism" filtered the list itself. The two filters
    are different questions and both belong here: `provenance` is whether the claim's quote was found in its
    stored source, `epistemic` is what other claims say about it (`supported`, `contradicted`, `contested`,
    `unresolved`). `None` means no filter, and an empty collection means nothing passes — which is a selection,
    not a mistake, so it is not silently widened.

    `aliases` maps an entity id to the id that stands for its group, which is how an entity deduplication
    (see `knowledge_base/dedup.py::resolve`) reaches the graph without rewriting the append-only store:
    the edges are relabelled on the way out, the records keep their own ids. Names follow the surviving id.

    `edge_filter` keeps the edges for which it returns true, after the two set filters. It exists because a
    predicate such as `is_proper` ("verified *or* supported, and not contradicted") cannot be written as the AND of
    a `provenance` set and an `epistemic` set.
    """
    edges = mechanism_graph(records, include_superseded=include_superseded)
    if provenance is not None:
        allowed = set(provenance)
        edges = [e for e in edges if e.provenance_status in allowed]
    if epistemic is not None:
        standing = set(epistemic)
        edges = [e for e in edges if e.epistemic_status in standing]
    if edge_filter is not None:
        edges = [e for e in edges if edge_filter(e)]
    if not aliases:
        return edges

    def name_of(entity_id: str, fallback: str) -> str:
        entity = records.get(entity_id)
        return entity.canonical_name if isinstance(entity, Entity) else fallback

    relabelled: list[MechanismEdge] = []
    for edge in edges:
        cause, effect = aliases.get(edge.cause_id, edge.cause_id), aliases.get(edge.effect_id, edge.effect_id)
        # A claim whose cause and effect were merged into one node asserts that a thing causes itself. It is
        # not an edge of a mechanism, and keeping it would let a self-loop satisfy a "the edges join" check.
        if cause == effect:
            continue
        relabelled.append(
            replace(
                edge,
                cause_id=cause,
                cause_name=name_of(cause, edge.cause_name),
                effect_id=effect,
                effect_name=name_of(effect, edge.effect_name),
            )
        )
    return relabelled


def mechanism_path(
    records: Mapping[str, Record],
    *,
    include_superseded: bool = False,
    provenance: Collection[str] | None = None,
    epistemic: Collection[EpistemicStatus] | None = None,
    aliases: Mapping[str, str] | None = None,
    require_verified: bool = True,
    min_edges: int = 2,
    edge_filter: Callable[[MechanismEdge], bool] | None = None,
) -> list[MechanismEdge]:
    """The longest directed chain through the causal graph, as an ordered edge list; `[]` if there is none.

    This is the shape output A is asked for: "a causal model of the C9orf72-immune-neurodegeneration
    **pathway**" (the specification, p. 5). `mechanism_graph` returns a *set* of edges, and a set of edges that
    happens to be disconnected is a list of causes — which is exactly what an earlier run's mechanism gate
    reported. A caller that cites what this function returns cites a path by construction.

    Selection: with `require_verified` (the default), only a path holding at least one `verified` edge is a
    candidate, and the longest of those wins — `pipeline/section_gates.py::check_mechanism` requires both the
    join and one verified step, so a longer chain every step of which is an unchecked agent assertion is a
    path a caller would cite and the gate would then reject. Without it, the longest chain wins outright. Ties
    go to the path whose edges sort first by claim id, so the result is reproducible.

    `min_edges` is the floor below which a result is not a chain at all; 2 is the smallest join. Paths are
    simple — no entity is visited twice — so a cycle in the asserted graph cannot produce an unbounded walk,
    and the longest chain through a cycle of n nodes has n-1 edges rather than n.
    """
    edges = causal_edges(
        records,
        include_superseded=include_superseded,
        provenance=provenance,
        epistemic=epistemic,
        aliases=aliases,
        edge_filter=edge_filter,
    )
    outgoing: dict[str, list[MechanismEdge]] = {}
    for edge in sorted(edges, key=lambda e: e.claim_id):
        outgoing.setdefault(edge.cause_id, []).append(edge)

    best: list[MechanismEdge] = []

    def admissible(path: list[MechanismEdge]) -> bool:
        if len(path) < min_edges:
            return False
        return not require_verified or any(e.provenance_status == "verified" for e in path)

    def walk(node: str, visited: set[str], path: list[MechanismEdge]) -> None:
        nonlocal best
        if len(path) > len(best) and admissible(path):
            best = list(path)
        for edge in outgoing.get(node, ()):
            if edge.effect_id in visited:
                continue
            visited.add(edge.effect_id)
            path.append(edge)
            walk(edge.effect_id, visited, path)
            path.pop()
            visited.discard(edge.effect_id)

    for start in sorted(outgoing):
        walk(start, {start}, [])
    return best


HYPOTHESISED: Final = ("unverified", "partial")


@dataclass(frozen=True)
class MechanismSubgraph:
    """A path and what hangs off it: the backbone plus the one-hop edges kept around each of its nodes.

    `backbone` is ordered — it is a path, and the order is the mechanism. `neighbourhood` is not: it is the
    set of adjacent edges that passed the neighbour filter, in claim-id order. The two lists never share an
    edge, so a reader counting edges does not count a backbone step twice.
    """

    backbone: list[MechanismEdge]
    neighbourhood: list[MechanismEdge]

    @property
    def node_ids(self) -> list[str]:
        """Backbone nodes in path order, then the neighbourhood's outer ends, sorted."""
        spine = [self.backbone[0].cause_id] + [e.effect_id for e in self.backbone] if self.backbone else []
        outer = {e.cause_id for e in self.neighbourhood} | {e.effect_id for e in self.neighbourhood}
        return spine + sorted(outer - set(spine))

    @property
    def edges(self) -> list[MechanismEdge]:
        return self.backbone + self.neighbourhood


def mechanism_subgraph(
    records: Mapping[str, Record],
    *,
    include_superseded: bool = False,
    provenance: Collection[str] | None = None,
    epistemic: Collection[EpistemicStatus] | None = None,
    aliases: Mapping[str, str] | None = None,
    require_verified: bool = True,
    min_edges: int = 2,
    neighbour_provenance: Collection[str] | None = HYPOTHESISED,
    neighbour_epistemic: Collection[EpistemicStatus] | None = None,
    edge_filter: Callable[[MechanismEdge], bool] | None = None,
    neighbour_filter: Callable[[MechanismEdge], bool] | None = None,
) -> MechanismSubgraph:
    """`mechanism_path` as a backbone, plus the one-hop neighbourhood of each of its nodes.

    The path alone answers the gate and reads as a claim about *the* mechanism, which is more than the base
    supports: around every step there are adjacent causal claims nobody has checked, and they are what makes
    the chain one reading among several rather than a conclusion. So the backbone is drawn with the strict
    filters (`provenance`, `epistemic`, `require_verified`) and the neighbourhood with its own, deliberately
    looser ones — a subgraph whose two parts are held to the same standard shows nothing the path does not.

    The two neighbour filters are the same two axes as in `causal_edges`, applied to the adjacent edges only:

    - `neighbour_provenance` — whether the adjacent claim's quote was found in its source. The default
      `HYPOTHESISED` is `("unverified", "partial")`, which is this base's way of saying *hypothesis or
      supposition*: the KB has no such epistemic status, because status is computed from what other claims
      say about a claim, not from how confidently it was stated (see this module's header). An agent's
      unchecked causal assertion is `provenance_status == "unverified"`.
    - `neighbour_epistemic` — what other claims say about it; `None` by default, so a contested neighbour is
      shown rather than hidden. Contested neighbours are the most informative ones.

    Either filter set to `None` admits everything on that axis and to an empty collection admits nothing,
    exactly as in `causal_edges`. An edge belongs to the neighbourhood only if it is adjacent to a backbone
    node and is not itself a backbone step; an edge between two backbone nodes that the path did not take
    **is** a neighbour, since it is a competing adjacency and not part of the chain.
    """
    backbone = mechanism_path(
        records,
        include_superseded=include_superseded,
        provenance=provenance,
        epistemic=epistemic,
        aliases=aliases,
        require_verified=require_verified,
        min_edges=min_edges,
        edge_filter=edge_filter,
    )
    if not backbone:
        return MechanismSubgraph([], [])

    spine = {backbone[0].cause_id, *(e.effect_id for e in backbone)}
    taken = {e.claim_id for e in backbone}
    candidates = causal_edges(
        records,
        include_superseded=include_superseded,
        provenance=neighbour_provenance,
        epistemic=neighbour_epistemic,
        aliases=aliases,
        edge_filter=neighbour_filter,
    )
    neighbourhood = [
        e
        for e in sorted(candidates, key=lambda e: e.claim_id)
        if e.claim_id not in taken and (e.cause_id in spine or e.effect_id in spine)
    ]
    return MechanismSubgraph(backbone, neighbourhood)


def proper_mechanism(
    records: Mapping[str, Record],
    *,
    include_superseded: bool = False,
    aliases: Mapping[str, str] | None = None,
    min_edges: int = 1,
) -> MechanismSubgraph:
    """The mechanism of ADR 0033: the longest connected chain of proper edges, plus its improper neighbours.

    `backbone` holds only proper edges (`is_proper`). `neighbourhood` holds only improper edges (`is_improper`) that
    touch a backbone node: the hypothesis step. A contradicted or contested edge is in neither, so it does not
    appear in the view; the records keep it, and the `contradicts` relations that make it so.

    `min_edges` is a floor, default 1. The longest chain wins, so a chain of two or more edges is returned whenever the
    knowledge base holds one: there is no separate "prefer two, accept one" step. Backbone choice ties go to the path
    whose edges sort first by claim id, as in `mechanism_path`.
    """
    return mechanism_subgraph(
        records,
        include_superseded=include_superseded,
        aliases=aliases,
        require_verified=False,
        min_edges=min_edges,
        neighbour_provenance=None,
        edge_filter=is_proper,
        neighbour_filter=is_improper,
    )
