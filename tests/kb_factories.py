"""Builders of valid knowledge-base records for the tests; each field can be overridden."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from knowledge_base.schemas import (
    Claim,
    Decision,
    Entity,
    Experiment,
    Hypothesis,
    Ignorance,
    Intervention,
    Relation,
    canonical_hash,
)

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
CAUSAL = {
    "cause_ref": "N-1",
    "effect_ref": "N-2",
    "context": "ALS/FTD",
    "mechanism": "repeat-associated non-AUG translation",
}


def claim(**overrides: Any) -> Claim:
    fields: dict[str, Any] = {
        "id": "C-1",
        "type": "observational",
        "content": "DPRs are detected in C9-ALS spinal cord.",
        "origin": "literature",
        "provenance_status": "verified",
        "verification_method": "quote",
        "source_refs": ["doi:10.1038/x"],
        "run_id": "RUN-1",
        "created_at": NOW,
    }
    fields.update(overrides)
    if "canonical_hash" not in fields:
        # The causal block, not the type, decides whether the hash covers the edge.
        causal = {k: fields[k] for k in CAUSAL} if all(fields.get(k) is not None for k in CAUSAL) else None
        fields["canonical_hash"] = canonical_hash(fields["content"], causal)
    return Claim(**fields)


def relation(**overrides: Any) -> Relation:
    fields: dict[str, Any] = {
        "id": "L-1",
        "source_id": "C-1",
        "target_id": "C-2",
        "type": "supports",
        "justification_method": "quote",
        "provenance_status": "verified",
        "source_refs": ["doi:10.1038/x"],
        "run_id": "RUN-1",
        "created_at": NOW,
    }
    fields.update(overrides)
    return Relation(**fields)


def entity(**overrides: Any) -> Entity:
    fields: dict[str, Any] = {"id": "N-1", "canonical_name": "C9orf72 expansion", "run_id": "RUN-1", "created_at": NOW}
    fields.update(overrides)
    return Entity(**fields)


def hypothesis(**overrides: Any) -> Hypothesis:
    fields: dict[str, Any] = {
        "id": "H-1",
        "statement": "s",
        "mechanism": "m",
        "author_confidence": 3,
        "agent_id": "A1",
        "run_id": "RUN-1",
        "created_at": NOW,
    }
    fields.update(overrides)
    return Hypothesis(**fields)


def ignorance(**overrides: Any) -> Ignorance:
    fields: dict[str, Any] = {
        "id": "I-1",
        "content": "Is the DPR load causal or a marker?",
        "agent_id": "A1",
        "run_id": "RUN-1",
        "created_at": NOW,
    }
    fields.update(overrides)
    return Ignorance(**fields)


def experiment(**overrides: Any) -> Experiment:
    fields: dict[str, Any] = {
        "id": "EXP-1",
        "hypothesis_id": "H-1",
        "title": "Treg depletion in a C9 mouse model",
        "protocol": "Deplete Tregs and measure motor-neuron counts.",
        "tested_claims": ["C-1"],
        "expected_outcomes": [
            {"outcome": "neuron loss accelerates", "claim_id": "C-1", "effect": "supports"},
            {"outcome": "no change", "claim_id": "C-1", "effect": "contradicts"},
        ],
        "cost": "medium",
        "cost_rationale": "mouse cohort",
        "feasibility": "medium",
        "agent_id": "designer-1",
        "kind": "wet_lab",
        "run_id": "RUN-1",
        "created_at": NOW,
    }
    fields.update(overrides)
    return Experiment(**fields)


def decision(**overrides: Any) -> Decision:
    fields: dict[str, Any] = {
        "id": "D-1",
        "hypothesis_id": "H-1",
        "decision": "buy",
        "experiment_ids": ["EXP-1"],
        "value_of_information": "high",
        "rationale": "decides the causal step",
        "agent_id": "arbiter-1",
        "run_id": "RUN-1",
        "created_at": NOW,
    }
    fields.update(overrides)
    return Decision(**fields)


def intervention(**overrides: Any) -> Intervention:
    fields: dict[str, Any] = {
        "id": "INT-1",
        "hypothesis_id": "H-1",
        "name": "Anti-CD4 depletion",
        "modality": "biologic",
        "target": "CD4 T cells",
        "acts_on_claims": ["C-1"],
        "rationale": "removes the effector population",
        "predicted_effect": "slower motor decline",
        "off_target_risks": ["broad immunosuppression"],
        "safety_concerns": ["infections"],
        "stratification": "carriers with C9orf72-reactive CD4 T cells",
        "biomarker": "C9orf72-specific CD4 T-cell frequency",
        "maturity": "medium",
        "agent_id": "intervener-1",
        "run_id": "RUN-1",
        "created_at": NOW,
    }
    fields.update(overrides)
    return Intervention(**fields)
