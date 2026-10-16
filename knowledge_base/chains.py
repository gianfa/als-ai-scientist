"""The causal chains of a final proposal, read from the knowledge base and written as Markdown lines.

Deterministic: no model is called, and nothing is taken from the PI's prose. `pipeline/synthesis.py::render` puts the
result into `final-proposal.md` when `synthesize` ends, and `evaluation/proposal_readable.py` into the readable
document. A chain is the longest directed chain among the causal claims in question, in order; every step says whether
the mechanism view counts it as proper, improper or hidden (ADR 0033).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .schemas import Claim, Hypothesis, Relation
from .status import MechanismEdge, is_improper, is_proper, mechanism_graph, superseded

# What a reader needs to read the section; shared by every document that carries it.
CHAINS_NOTE = [
    "Written deterministically from the knowledge base (`knowledge_base/chains.py`), not by the system's PI. A chain is "
    "the longest directed chain among the causal claims in question, in order; **proper** means the claim's quote was "
    "verified against its source or a verified relation supports it, **improper** is an unchecked assertion, and a "
    "claim that other verified evidence contradicts or contests is hidden from the mechanism view (ADR 0033). A chain "
    "is a reading of asserted claims, not a causal model identified from data.",
    "",
]

# The exhaustive search for the longest simple chain is exponential in the worst case; a hypothesis or a section cites a
# handful of edges, so the cap is never reached in practice, and past it the edges are listed unordered and say so.
CHAIN_SEARCH_LIMIT = 30


def _kind(edge: MechanismEdge | None) -> str:
    """How the mechanism view treats an edge (ADR 0033): proper, improper, or hidden (contradicted or contested)."""
    if edge is None:
        return "not a current causal claim (superseded, or not causal)"
    if is_proper(edge):
        return "proper"
    if is_improper(edge):
        return "improper"
    return f"hidden from the view ({edge.epistemic_status})"


def _longest_chain(edges: list[MechanismEdge]) -> list[MechanismEdge] | None:
    """The longest directed simple chain among `edges`, ties to the lowest claim ids; `None` past the search cap."""
    if len(edges) > CHAIN_SEARCH_LIMIT:
        return None
    outgoing: dict[str, list[MechanismEdge]] = {}
    for edge in sorted(edges, key=lambda e: e.claim_id):
        outgoing.setdefault(edge.cause_id, []).append(edge)
    best: list[MechanismEdge] = []

    def walk(node: str, seen: set[str], path: list[MechanismEdge]) -> None:
        nonlocal best
        if len(path) > len(best):
            best = list(path)
        for edge in outgoing.get(node, ()):
            if edge.effect_id not in seen:
                walk(edge.effect_id, seen | {edge.effect_id}, path + [edge])

    for start in sorted(outgoing):
        walk(start, {start}, [])
    return best


def _step(edge: MechanismEdge) -> str:
    return (
        f"`{edge.claim_id}` {edge.cause_name} -> {edge.effect_name} — {_kind(edge)}; "
        f"provenance `{edge.provenance_status}`, epistemic `{edge.epistemic_status}`"
    )


def _edge_block(title: str, claim_ids: list[str], view: Mapping[str, MechanismEdge]) -> list[str]:
    """One list of causal claims: the longest chain they form, in order, then the rest, each with its standing."""
    in_view = [view[i] for i in claim_ids if i in view]
    shown = [e for e in in_view if is_proper(e) or is_improper(e)]
    hidden = [e for e in in_view if e not in shown]
    absent = [i for i in claim_ids if i not in view]
    chain = _longest_chain(shown)
    out = [f"**{title}**", ""]
    if chain is None:
        out += [f"- {len(shown)} causal claims, listed unordered (more than {CHAIN_SEARCH_LIMIT} to order):"]
        out += [f"  - {_step(e)}" for e in shown]
    elif not chain:
        out += ["- no causal claim"]
    else:
        out += [f"{n}. {_step(e)}" for n, e in enumerate(chain, 1)]
        on_chain = {e.claim_id for e in chain}
        off = [e for e in shown if e.claim_id not in on_chain]
        if off:
            out += ["", "Off this chain:", ""] + [f"- {_step(e)}" for e in off]
    if hidden:
        out += ["", "Hidden from the view, not part of any chain above:", ""] + [f"- {_step(e)}" for e in hidden]
    if absent:
        out += ["", "Cited but not a current causal claim of the view: " + ", ".join(f"`{i}`" for i in absent) + "."]
    return out + [""]


def causal_chains(records: Mapping[str, Any], run_id: str, proposal: Mapping[str, Any]) -> list[str]:
    """The causal chains of the final proposal, as Markdown lines: section A's cited claims, then each final hypothesis.

    Deterministic, read from the knowledge base alone. A chain is the longest directed chain among the causal claims in
    question; every step says whether the mechanism view counts it as proper, improper or hidden (ADR 0033). The
    final hypotheses are the live `H-` records of this run and any `H-` the proposal cites.
    """
    view = {e.claim_id: e for e in mechanism_graph(records)}
    old = superseded(records)
    section = proposal.get("mechanism")
    cites = list(section.get("cites") or []) if isinstance(section, Mapping) else []
    cited_hypotheses = [i for i in cites if i.startswith("H-")]
    for value in proposal.values():
        if isinstance(value, Mapping):
            cited_hypotheses += [i for i in value.get("cites") or [] if i.startswith("H-")]
    hypotheses = sorted(
        {h.id for h in records.values() if isinstance(h, Hypothesis) and h.run_id == run_id and h.id not in old}
        | {i for i in cited_hypotheses if isinstance(records.get(i), Hypothesis)},
        key=lambda i: int(i.split("-")[1]),
    )
    members: dict[str, list[str]] = {h: [] for h in hypotheses}
    for r in records.values():
        if isinstance(r, Relation) and r.type == "part_of" and r.target_id in members:
            members[r.target_id].append(r.source_id)

    out: list[str] = []
    causal_cites = [
        i for i in cites if i in view or (isinstance(records.get(i), Claim) and records[i].causal_fields() is not None)
    ]
    out += _edge_block("Section A: the causal claims it cites", causal_cites, view)
    for h in hypotheses:
        claims = sorted(members[h], key=lambda i: int(i.split("-")[1]))
        causal = [i for i in claims if i in view]
        header = f"{h}: {len(claims)} claims, {len(causal)} causal"
        out += _edge_block(header, causal, view)
    if not hypotheses:
        out += ["This run holds no live hypothesis and the proposal cites none.", ""]
    return out
