"""Reusable per-stage checks over saved workflow records.

Pure functions: they inspect records and report problems; they never modify their input.
Required fields mirror the agent output schemas in `agents/*.js` plus workflow-assigned IDs.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "evidence": ("id", "identifier", "quote", "stance", "source_type"),
    "hypotheses": (
        "id",
        "mechanism",
        "statement",
        "supporting",
        "contradicting",
        "assumptions",
        "predictions",
        "status",
    ),
    "interventions": (
        "id",
        "name",
        "modality",
        "target",
        "rationale",
        "supporting_evidence_ids",
        "contradicting_evidence_ids",
        "known_risks",
        "supporting_hypothesis_ids",
    ),
    "experiments": (
        "id",
        "hypothesis_id",
        "objective",
        "model",
        "intervention",
        "controls",
        "readouts",
        "expected_if_true",
        "expected_if_false",
        "falsification_criterion",
        "supporting_evidence_ids",
    ),
}

# Outcome of the proposal-producing path, and fields validateProposal requires for it (A-E, G, H).
PROPOSAL_FIELDS: dict[str, str] = {
    "A": "disease_mechanism",
    "B": "therapeutic_hypothesis",
    "C": "intervention",
    "D": "patient_stratification",
    "E": "biomarker",
    "G": "preclinical_to_clinical_translation_path",
}


def is_filled(value: Any) -> bool:
    """True for a non-empty string/collection or any other non-None value."""
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict, set)):
        return bool(value)
    return True


def missing_fields(record: Mapping[str, Any], fields: Iterable[str]) -> list[str]:
    """Fields absent from `record`. Empty lists count as present (e.g. no contradicting evidence)."""
    return [
        f
        for f in fields
        if f not in record or record[f] is None or (isinstance(record[f], str) and not record[f].strip())
    ]


def stage_field_violations(stage: str, records: Iterable[Mapping[str, Any]]) -> list[str]:
    """One message per record of `stage` that lacks a required field."""
    fields = REQUIRED_FIELDS[stage]
    out = []
    for i, record in enumerate(records):
        if not isinstance(record, Mapping):
            out.append(f"{stage}[{i}]: not an object")
            continue
        missing = missing_fields(record, fields)
        if missing:
            out.append(f"{stage}[{record.get('id', i)}]: missing {','.join(missing)}")
    return out


def unresolved_ids(cited: Iterable[str], known: Iterable[str]) -> list[str]:
    """Cited IDs not present in `known`, sorted and deduplicated."""
    return sorted(set(cited) - set(known))


def is_admitted_evidence(item: Mapping[str, Any]) -> bool:
    """Evidence admitted to reasoning at runtime: checker-verified literature or verified computation."""
    check = item.get("evidence_check") or {}
    if check.get("status") == "verified":
        return True
    verification = item.get("verification") or {}
    return str(item.get("id", "")).startswith("C") and verification.get("verified") is True
