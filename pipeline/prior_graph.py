"""The prior causal graph: the scaffold the literature of a run leaves for the hypotheses that follow.

Two objects are easy to confuse, and this module is only the second one:

- the **knowledge-base graph** (`knowledge_base/status.py::mechanism_graph`) mirrors every causal claim the system
  has ever written, with its computed status. It is a view, it grows as a run proceeds, and it constrains nothing;
- the **prior** is a *selection*: which of those edges, read from the sources at the start, form the structure a
  hypothesis is expected to attach to. Selecting is a modelling choice, not evidence.

The division: what a paper says is evidence and belongs in the
append-only knowledge base, where it can be superseded but never removed; which edges constitute the prior is a
choice that may be revised, so it lives in a versioned artifact that **references claim IDs** and owns no content of its
own. Changing the prior is then a new version of a file, not a retraction in a store that has no retraction.

Selection rule v1, deliberately mechanical: the causal claims of this run that carry a source and whose quote was
verified against it, taken **at the end of the literature phase**. A claim does not record which agent wrote it,
so the moment of construction is part of the rule: the artifact is built once, right after `literature`, and a
resumed run reuses the file rather than reselecting over a knowledge base the later phases have grown. No agent
judges what belongs; a curation step can be added later, and would change `RULE` and the version, which is
exactly what the version is for.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from knowledge_base.schemas import Claim
from knowledge_base.status import statuses
from knowledge_base.validate import Record

VERSION = "1"
RULE = (
    "causal claims of this run with a verified source quote, selected at the end of the literature phase; "
    "deterministic, no agent curates it"
)
DIGEST_PREFIX = 12


def select(records: Mapping[str, Record], run_id: str) -> list[Claim]:
    """The claims of the prior, in record order. Empty is a legitimate answer and must not be hidden.

    `origin == "literature"` means the claim came with a source; a claim an agent asserted from its own knowledge
    is `agent` and cannot enter a scaffold meant to carry only what a source states.
    """
    return [
        r
        for r in records.values()
        if isinstance(r, Claim)
        and r.causal_fields() is not None
        and r.run_id == run_id
        and r.origin == "literature"
        and r.provenance_status == "verified"
    ]


def build(records: Mapping[str, Record], run_id: str, objective: str) -> dict[str, Any]:
    """The versioned artifact: the rule, the selected edges by ID, and a digest of the selection.

    The digest covers the rule and the selected IDs, so two runs that select the same edges under the same rule
    cite the same prior, and a changed selection is visible as a different version string.
    """
    status = statuses(records)
    chosen = select(records, run_id)
    edges = [
        {
            "claim_id": c.id,
            "cause_id": c.cause_ref,
            "effect_id": c.effect_ref,
            "context": c.context,
            "mechanism": c.mechanism,
            "content": c.content,
            "source_refs": list(c.source_refs),
            "epistemic_status": status[c.id],
            "cause_name": _name(records, c.cause_ref),
            "effect_name": _name(records, c.effect_ref),
        }
        for c in chosen
    ]
    payload = {"rule": RULE, "claim_ids": [c.id for c in chosen]}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {
        "version": VERSION,
        "ref": f"prior:v{VERSION}@{digest[:DIGEST_PREFIX]}",
        "rule": RULE,
        "run_id": run_id,
        "objective": objective,
        "edges": edges,
        "total": len(edges),
        "note": "A selection of verified causal claims, not a causal model identified from data. An edge "
        "whose epistemic_status is unresolved is a starting point to test, not a fact.",
    }


def _name(records: Mapping[str, Record], entity_id: str | None) -> str:
    entity = records.get(entity_id or "")
    return getattr(entity, "canonical_name", entity_id or "")


def for_prompt(prior: Mapping[str, Any], limit: int = 40) -> list[dict[str, Any]]:
    """The compact form packed into the hypothesis authors' arguments: enough to attach to, not the whole record."""
    # `.get`, because the artifact may have been written by an earlier version of this module.
    return [
        {
            "claim_id": e.get("claim_id"),
            "cause": e.get("cause_name"),
            "effect": e.get("effect_name"),
            "mechanism": e.get("mechanism"),
            "status": e.get("epistemic_status"),
        }
        for e in list(prior.get("edges", []))[:limit]
    ]


__all__ = ["DIGEST_PREFIX", "RULE", "VERSION", "build", "for_prompt", "select"]
