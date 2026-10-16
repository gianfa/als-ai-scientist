"""Citation extraction and resolution over saved run records.

Deterministic string/ID operations only. Whether a source actually supports a claim needs an
independent check or ground truth and is not decided here (see metrics.py).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

EVIDENCE_ID_RE = re.compile(r"\b[EC]\d+\b")
EXPERIMENT_ID_RE = re.compile(r"\bEXP\d+(?:-\d+)?\b")
INTERVENTION_ID_RE = re.compile(r"\bI\d+\b")


def ids_in_text(text: str | None, pattern: re.Pattern[str] = EVIDENCE_ID_RE) -> list[str]:
    return sorted(set(pattern.findall(text or "")))


def supporting_citations(result: Mapping[str, Any]) -> dict[str, list[str]]:
    """Evidence IDs used as support, keyed by the citing artifact (e.g. `hypothesis:H1`).

    Only reasoning artifacts that reached downstream stages are included: rejected hypotheses are
    retained for provenance but are not required to cite admitted evidence.
    """
    out: dict[str, list[str]] = {}
    for h in result.get("hypotheses") or []:
        if h.get("status") in ("surviving", "revised"):
            out[f"hypothesis:{h.get('id')}"] = list(h.get("supporting") or [])
    for c in result.get("interventions") or []:
        out[f"intervention:{c.get('id')}"] = list(c.get("supporting_evidence_ids") or [])
    for e in result.get("experiments") or []:
        out[f"experiment:{e.get('id')}"] = list(e.get("supporting_evidence_ids") or [])
    if result.get("outcome") == "proposal":
        out["proposal_markdown"] = ids_in_text(result.get("proposal_markdown"))
    return out


def contradicting_citations(result: Mapping[str, Any]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for h in result.get("hypotheses") or []:
        out[f"hypothesis:{h.get('id')}"] = list(h.get("contradicting") or [])
    for c in result.get("interventions") or []:
        out[f"intervention:{c.get('id')}"] = list(c.get("contradicting_evidence_ids") or [])
    return out
