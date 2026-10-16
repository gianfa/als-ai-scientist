"""Provenance-chain validation: final claim -> agent artifact -> evidence ID -> source/computation."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .citations import EXPERIMENT_ID_RE, contradicting_citations, ids_in_text, supporting_citations
from .local import is_admitted_evidence, unresolved_ids


def provenance_problems(result: Mapping[str, Any]) -> list[str]:
    """Broken links in the saved workflow result. Empty list means the chain is intact."""
    evidence = [e for e in result.get("evidence") or [] if isinstance(e, Mapping)]
    known = {e.get("id") for e in evidence}
    admitted = {e.get("id") for e in evidence if is_admitted_evidence(e)}
    hypotheses = {h.get("id"): h for h in result.get("hypotheses") or [] if isinstance(h, Mapping)}
    survivors = {hid for hid, h in hypotheses.items() if h.get("status") in ("surviving", "revised")}
    experiments = {e.get("id") for e in result.get("experiments") or [] if isinstance(e, Mapping)}
    interventions = {c.get("id") for c in result.get("interventions") or [] if isinstance(c, Mapping)}
    problems: list[str] = []

    for where, ids in supporting_citations(result).items():
        if bad := unresolved_ids(ids, admitted):
            problems.append(f"{where}: supporting IDs not admitted evidence ({','.join(bad)})")
    for where, ids in contradicting_citations(result).items():
        if bad := unresolved_ids(ids, known):
            problems.append(f"{where}: contradicting IDs not in the evidence ledger ({','.join(bad)})")
    for h in hypotheses.values():
        if h.get("parent_id") and h["parent_id"] not in hypotheses:
            problems.append(f"hypothesis:{h.get('id')}: unknown parent_id {h['parent_id']}")
    for c in result.get("interventions") or []:
        if bad := unresolved_ids(c.get("supporting_hypothesis_ids") or [], survivors):
            problems.append(f"intervention:{c.get('id')}: unknown or non-surviving hypotheses ({','.join(bad)})")
    for e in result.get("experiments") or []:
        if e.get("hypothesis_id") not in survivors:
            problems.append(f"experiment:{e.get('id')}: unknown or non-surviving hypothesis {e.get('hypothesis_id')}")

    proposal = result.get("proposal") or {}
    cited_experiments = set(proposal.get("experiment_ids") or []) | set(
        ids_in_text(result.get("proposal_markdown"), EXPERIMENT_ID_RE)
    )
    if bad := unresolved_ids(cited_experiments, experiments):
        problems.append(f"proposal: unknown experiment IDs ({','.join(bad)})")
    if bad := unresolved_ids(proposal.get("intervention_candidate_ids") or [], interventions):
        problems.append(f"proposal: unknown intervention IDs ({','.join(bad)})")
    return problems
