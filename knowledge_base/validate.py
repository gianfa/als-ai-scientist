"""Rules that need more than one record. Pure functions, no I/O.

`problems(record, known)` lists why a proposed record cannot enter the KB, given the records already in it
(`known`, ID -> record). An empty list means it may be written. The caller (the single writer) turns a non-empty
list into a `rejected` event; nothing here raises on a bad proposal, so one bad record does not stop a batch.
Rules local to one record are enforced earlier, by the Pydantic models in `schemas`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final, Union

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
    RelationType,
    normalize,
)

Record = Union[Claim, Relation, Entity, Hypothesis, Ignorance, Event, Experiment, Decision, Intervention]

# Which record types each relation admits as (source, target), by ID prefix. `tests` starts from an EXP- record
# only: an experiment is not a claim.
ENDPOINTS: Final[dict[RelationType, tuple[frozenset[str], frozenset[str]]]] = {
    "supports": (frozenset("C"), frozenset("C")),
    "contradicts": (frozenset("C"), frozenset("C")),
    "questions": (frozenset("I"), frozenset("CH")),
    "tests": (frozenset({"EXP"}), frozenset("C")),
    "part_of": (frozenset("C"), frozenset("H")),
    "derived_from": (frozenset("C"), frozenset("C")),
    "depends_on": (frozenset({"C", "H", "INT"}), frozenset("C")),
    "supersedes": (frozenset("CHI"), frozenset("CHI")),
}


def prefix(record_id: str) -> str:
    return record_id.split("-", 1)[0]


def problems(record: Record, known: Mapping[str, Record]) -> list[str]:
    found: list[str] = []
    if record.id in known:
        found.append(f"{record.id} already exists")
    if isinstance(record, Claim):
        found += _claim(record, known)
    elif isinstance(record, Relation):
        found += _relation(record, known)
    elif isinstance(record, Entity):
        found += _entity(record, known)
    elif isinstance(record, Ignorance):
        found += _missing(record.hypothesis_id, Hypothesis, known, f"ignorance {record.id}")
    elif isinstance(record, Experiment):
        found += _missing(record.hypothesis_id, Hypothesis, known, f"experiment {record.id}")
        for cid in record.tested_claims:
            found += _missing(cid, Claim, known, f"experiment {record.id}")
    elif isinstance(record, Decision):
        found += _missing(record.hypothesis_id, Hypothesis, known, f"decision {record.id}")
        for eid in record.experiment_ids:
            found += _missing(eid, Experiment, known, f"decision {record.id}")
            experiment = known.get(eid)
            if isinstance(experiment, Experiment) and experiment.hypothesis_id != record.hypothesis_id:
                found.append(f"decision {record.id} buys {eid}, which belongs to {experiment.hypothesis_id}")
    elif isinstance(record, Intervention):
        found += _missing(record.hypothesis_id, Hypothesis, known, f"intervention {record.id}")
        for cid in record.acts_on_claims:
            found += _missing(cid, Claim, known, f"intervention {record.id}")
    elif isinstance(record, Event) and record.event != "rejected":  # a rejected record never entered the KB
        found += _missing(record.record_id, object, known, f"event {record.id}")
    return found


def _missing(ref: str | None, kind: type, known: Mapping[str, Record], owner: str) -> list[str]:
    if ref is None:
        return []
    target = known.get(ref)
    if target is None:
        return [f"{owner} refers to unknown {ref}"]
    if not isinstance(target, kind):
        return [f"{owner} refers to {ref}, which is a {type(target).__name__}"]
    return []


def _claim(claim: Claim, known: Mapping[str, Record]) -> list[str]:
    refs = [*claim.entity_refs, *(r for r in (claim.cause_ref, claim.effect_ref) if r is not None)]
    found: list[str] = []
    for ref in dict.fromkeys(refs):
        found += _missing(ref, Entity, known, f"claim {claim.id}")
    duplicate = next(
        (r.id for r in known.values() if isinstance(r, Claim) and r.canonical_hash == claim.canonical_hash), None
    )
    if duplicate is not None:
        found.append(
            f"claim {claim.id} duplicates {duplicate} (same canonical_hash): record a seen_again event instead"
        )
    return found


def _relation(relation: Relation, known: Mapping[str, Record]) -> list[str]:
    found: list[str] = []
    sources, targets = ENDPOINTS[relation.type]
    if prefix(relation.source_id) not in sources:
        found.append(f"{relation.type} cannot start from {relation.source_id} (allowed: {sorted(sources)})")
    if prefix(relation.target_id) not in targets:
        found.append(f"{relation.type} cannot point to {relation.target_id} (allowed: {sorted(targets)})")
    if relation.type == "supersedes" and prefix(relation.source_id) != prefix(relation.target_id):
        found.append(
            f"supersedes must link records of the same type, not {relation.source_id} and {relation.target_id}"
        )
    for ref in (relation.source_id, relation.target_id):
        if ref not in known:
            found.append(f"relation {relation.id} refers to unknown {ref}")
    source = known.get(relation.source_id)
    if relation.type == "contradicts" and isinstance(source, Claim) and source.provenance_status != "verified":
        found.append(f"contradicts {relation.id} starts from unverified {source.id}: record it as questions instead")
    return found


def _entity(entity: Entity, known: Mapping[str, Record]) -> list[str]:
    names = {normalize(n) for n in (entity.canonical_name, *entity.aliases)}
    for other in known.values():
        if isinstance(other, Entity) and other.id != entity.id:
            clash = names & {normalize(n) for n in (other.canonical_name, *other.aliases)}
            if clash:
                return [f"entity {entity.id} reuses the name(s) {sorted(clash)} of {other.id}"]
    return []
