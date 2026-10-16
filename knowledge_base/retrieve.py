"""Deterministic retrieval of KB slices for agents. Pure functions over `read_records(root)`.

Agents never receive the whole KB: a phase asks for a slice by ID, by graph neighbourhood or by keyword, and
`as_context` turns it into the compact JSON-ready form given to an agent, with each claim's computed status.
Events are bookkeeping and are never retrieved. Same KB and same query give the same slice, in record order.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from .schemas import (
    Claim,
    Decision,
    Entity,
    Event,
    Experiment,
    Hypothesis,
    Ignorance,
    Intervention,
    Relation,
    normalize,
)
from .status import narrowings, statuses, superseded
from .validate import Record


def _visible(records: Mapping[str, Record]) -> dict[str, Record]:
    return {rid: r for rid, r in records.items() if not isinstance(r, Event)}


def by_id(ids: Iterable[str], records: Mapping[str, Record]) -> dict[str, Record]:
    wanted = set(ids)
    missing = wanted - set(records)
    if missing:
        raise KeyError(f"unknown records {sorted(missing)}")
    return {rid: r for rid, r in _visible(records).items() if rid in wanted}


def _links(record: Record) -> list[str]:
    if isinstance(record, Relation):
        return [record.source_id, record.target_id]
    if isinstance(record, Claim):
        return [*record.entity_refs, *(r for r in (record.cause_ref, record.effect_ref) if r)]
    if isinstance(record, Ignorance) and record.hypothesis_id:
        return [record.hypothesis_id]
    if isinstance(record, Experiment):
        return [record.hypothesis_id]
    if isinstance(record, Decision):
        return [record.hypothesis_id, *record.experiment_ids]
    if isinstance(record, Intervention):
        return [record.hypothesis_id, *record.acts_on_claims]
    return []


def neighbourhood(seeds: Iterable[str], records: Mapping[str, Record], hops: int = 1) -> dict[str, Record]:
    """The seeds plus every record within `hops` steps. One step crosses one relation (source <-> target, the
    relation itself included) or one direct reference (claim -> entity, ignorance -> hypothesis)."""
    visible = _visible(records)
    adjacency: dict[str, set[str]] = {rid: set() for rid in visible}
    for rid, record in visible.items():
        for other in _links(record):
            if other in adjacency:
                if isinstance(record, Relation):  # the relation sits between its two endpoints
                    adjacency[other].add(rid)
                    adjacency[rid].add(other)
                else:
                    adjacency[rid].add(other)
                    adjacency[other].add(rid)
    frontier = set(by_id(seeds, records))
    reached = set(frontier)
    for _ in range(hops):
        step = set()
        for rid in frontier:
            for nxt in adjacency[rid]:
                # Crossing a relation reaches the relation and its far endpoint in the same step.
                step |= {nxt, *(_links(visible[nxt]) if isinstance(visible[nxt], Relation) else [])}
        frontier = {r for r in step if r in visible} - reached
        reached |= frontier
    return {rid: r for rid, r in visible.items() if rid in reached}


def _text(record: Record) -> str:
    if isinstance(record, Claim):
        return " ".join(filter(None, (record.content, record.context, record.mechanism)))
    if isinstance(record, Hypothesis):
        return f"{record.statement} {record.mechanism}"
    if isinstance(record, Ignorance):
        return record.content
    if isinstance(record, Entity):
        return " ".join((record.canonical_name, *record.aliases))
    if isinstance(record, Experiment):
        return f"{record.title} {record.protocol}"
    if isinstance(record, Decision):
        return record.rationale
    if isinstance(record, Intervention):
        return f"{record.name} {record.target} {record.rationale}"
    return ""


# Every record type `_text` can match, and the two a search looks at when it is not told otherwise. The default
# is narrow because a wide search answers agents mostly with the system's own output (experiments, hypotheses,
# decisions) and buries the few verified literature claims. The other types stay reachable by naming them in `types`.
SEARCHABLE_TYPES = ("Claim", "Entity", "Hypothesis", "Ignorance", "Experiment", "Decision", "Intervention")
DEFAULT_SEARCH_TYPES = ("Claim", "Entity")


def _search_types(types: Iterable[str] | str | None) -> frozenset[str]:
    """The record types a search may match: `DEFAULT_SEARCH_TYPES` when unasked, otherwise the named ones.
    An unknown or empty selection raises instead of matching nothing, so a mistyped name cannot read to the
    caller as "the knowledge base holds nothing like that"."""
    if types is None:
        return frozenset(DEFAULT_SEARCH_TYPES)
    names = [types] if isinstance(types, str) else list(types)
    known = {name.casefold(): name for name in SEARCHABLE_TYPES}
    if not names:
        raise ValueError(f"types must name at least one record type, one or more of {list(SEARCHABLE_TYPES)}")
    unknown = [name for name in names if name.casefold() not in known]
    if unknown:
        raise ValueError(f"unknown record types {unknown}; types must name one or more of " f"{list(SEARCHABLE_TYPES)}")
    return frozenset(known[name.casefold()] for name in names)


def keyword(
    query: str, records: Mapping[str, Record], limit: int = 20, types: Iterable[str] | str | None = None
) -> dict[str, Record]:
    """Records whose text contains every query word (normalized), best matches first by word occurrences,
    ties in record order. Relations have no text and are not matched.

    Only claims and entities are searched unless `types` names others (`SEARCHABLE_TYPES`); an unknown name
    raises."""
    wanted = _search_types(types)
    words = re.findall(r"\w+", normalize(query))
    if not words:
        return {}
    scored = []
    for order, (rid, record) in enumerate(_visible(records).items()):
        if type(record).__name__ not in wanted:
            continue
        tokens = re.findall(r"\w+", normalize(_text(record)))
        if tokens and all(w in tokens for w in words):
            scored.append((-sum(tokens.count(w) for w in words), order, rid, record))
    return {rid: record for _, _, rid, record in sorted(scored)[:limit]}


# Who wrote a record and how sure they were: withheld from every agent-facing view, because `review` is blinded
# (`workflows/phases/src/review.js`: "Other reviewers assess the same hypothesis without seeing your report, and
# you do not know who wrote it"). Stripping it only from the slice the orchestrator pushes into a prompt is not
# enough: the pulled tools (`kb_get`, `kb_neighbourhood`) would give it back to a reviewer reading its own
# hypothesis. One exclusion, in the one function every served tool goes through.
BLINDED = frozenset({"agent_id", "author_confidence"})


def as_context(slice_: Mapping[str, Record], records: Mapping[str, Record]) -> list[dict[str, Any]]:
    """The slice as compact dicts for an agent prompt: no hashes, timestamps or run IDs, no author fields
    (`BLINDED`); claims carry their computed `epistemic_status`, whether they are superseded, and — when the
    verifier judged one to say more than its quote — `narrowed_to`, the claims stating the quote's part alone.
    An earlier run's PI fetched C-19 and its narrowing C-95 together, was told nothing linked them, and cited
    C-19 for exactly the clause the verifier had removed."""
    status = statuses(records)
    old = superseded(records)
    narrowed = narrowings(records)
    out = []
    for rid, record in slice_.items():
        item = record.model_dump(exclude={"canonical_hash", "created_at", "run_id", *BLINDED}, exclude_none=True)
        if isinstance(record, Claim):
            item["epistemic_status"] = status[rid]
            item["superseded"] = rid in old
            if rid in narrowed:
                item["narrowed_to"] = narrowed[rid]
        out.append(item)
    return out
