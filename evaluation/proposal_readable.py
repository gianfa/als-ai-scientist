"""Render a run's final proposal as a document a person can read without the knowledge base open.

The proposal the PI writes cites records by tag — `[[C-849]]`, `[[EXP-66]]` — which is what makes it
checkable and what makes it unreadable. This renderer keeps the prose exactly as written and resolves
every tag against the archived knowledge base, so a reader sees what each citation actually says, where
it came from, and what the system itself thinks of it.

**It is deterministic.** No model is called. Every sentence it adds is a template filled from record
fields, and a field the archive does not hold is reported as absent rather than smoothed over. The
prose of the proposal is never rewritten: this is an annotation layer, not a second author.

    uv run python -m evaluation.proposal_readable <run-id> [--kb knowledgebase] [--out DIR]

Output: `experiments/final-proposals/<RUN_ID>.md` by default.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from knowledge_base.chains import CHAINS_NOTE, causal_chains
from knowledge_base.status import statuses
from knowledge_base.store import read_records
from tools.graph.build import build
from tools.graph.render import to_mermaid

REPO_ROOT = Path(__file__).resolve().parents[1]
# The graph keeps what a reader needs to follow a hypothesis to its support; entities and declared gaps are
# left out, because they outnumber the structure and would bury it.
GRAPH_KINDS = {"hypothesis", "claim", "experiment", "intervention", "decision"}
# Every shape and fill the diagram can draw is named here: a reader meeting a shape with no entry has to guess
# what it means, which is the one thing a legend exists to prevent.
GRAPH_LEGEND = [
    "- **Hexagon: a hypothesis** an agent proposed, with the claims it is built from attached to it.",
    "- **Box: a claim**, one statement the system can be held to. Its colour is not an opinion but a status "
    "computed from verified sources only: green, a verified source supports it; red, one contradicts it; amber, "
    "both do; white, neither does — so white means untested here, not false.",
    "- **Stadium: an experiment (blue) or an intervention (orange)** — the same shape, told apart by the fill. "
    "Both are *proposals*: no experiment in this graph was performed and no intervention was administered.",
    "- **Rhombus, grey: a decision** taken during the `arbitrate` stage by an agent that did not write the "
    "hypothesis, its reviews or its experiments. It judges whether the hypothesis is worth testing, not whether "
    "it is true: it accepts it and funds the experiments the rhombus points to, or it rejects it, or it stops "
    "work on it.",
    "- **Dashed arrow: a link no verified source backs** — the system asserted the relation but could not trace "
    "it to a source it had checked.",
    "- **Dashed outline: a record a later one replaced**; it is kept so the earlier reasoning stays readable.",
    "- **The word on an arrow is the kind of relation asserted.** Which kinds appear depends on the run; for "
    "example `part_of` (a claim the hypothesis is built from), `tests` (an experiment that would test a claim), "
    "`buys` (a decision funding an experiment) and `about` (the hypothesis a decision concerns).",
]
GRAPH_ID_NOTE = (
    "Each node shows the identifier of its record in the knowledge base (for example `C-1` for a claim, "
    "`H-1` for a hypothesis). The record is described under *Evidence cited* only if the proposal cites it."
)
GRAPH_CAVEAT = "The graph shows asserted records and their links, not a causal model identified from data."
TAG = re.compile(r"\[\[([A-Z]+-\d+)\]\]")

# How each record type is introduced in the evidence list. The wording states what the record *is*,
# because a reader meeting `I-473` for the first time cannot be expected to know.
KIND = {
    "C": "Claim",
    "H": "Hypothesis",
    "EXP": "Experiment (proposed)",
    "INT": "Intervention candidate",
    "I": "Declared gap",
    "D": "Arbiter decision",
    "L": "Relation",
    "N": "Entity",
}
# A status is a computed verdict over verified relations, so it is spelled out rather than printed raw.
STATUS_GLOSS = {
    "supported": "supported — at least one verified relation supports it and none contradicts it",
    "contradicted": "contradicted — at least one verified relation contradicts it and none supports it",
    "contested": "contested — verified relations both support and contradict it",
    "unresolved": "unresolved — no verified relation bears on it either way",
}


class ProposalMissing(RuntimeError):
    """The run has no final proposal: it did not reach synthesis."""


@dataclass(frozen=True)
class Flags:
    """What the proposal's own gate said about a cited record, keyed by record id."""

    contradicted_or_contested: dict[str, list[str]]
    judged_unfaithful: dict[str, list[str]]
    quoting_unnamed_source: dict[str, list[str]]

    @classmethod
    def read(cls, gate: Mapping[str, Any]) -> "Flags":
        def collect(field: str) -> dict[str, list[str]]:
            out: dict[str, list[str]] = {}
            for section, entries in (gate.get(field) or {}).items():
                for entry in entries:
                    out.setdefault(entry.split(" ", 1)[0], []).append(section)
            return out

        return cls(
            collect("cites_contradicted_or_contested"),
            collect("cites_judged_unfaithful"),
            collect("cites_quoting_an_unnamed_source"),
        )


def _sentence(text: str) -> str:
    """Normalise a record's content to one readable sentence, without changing its words."""
    text = " ".join((text or "").split())
    if text and text[-1] not in ".?!":
        text += "."
    return text


def _sources(record: Any) -> str:
    refs = list(getattr(record, "source_refs", None) or [])
    named = [r for r in refs if not r.startswith("receipt:")]
    receipts = [r for r in refs if r.startswith("receipt:")]
    if not named and not receipts:
        return "No source is named on this record."
    parts = []
    if named:
        parts.append("Source: " + ", ".join(named) + ".")
    if receipts:
        parts.append(f"A verification receipt is recorded ({receipts[0]}).")
    return " ".join(parts)


def _relation_sentences(rid: str, records: Mapping[str, Any], status: Mapping[str, str]) -> list[str]:
    """One sentence per verified relation bearing on this record, in plain prose."""
    out = []
    for rec in records.values():
        if type(rec).__name__ != "Relation" or getattr(rec, "provenance_status", "") != "verified":
            continue
        if rec.target_id != rid:
            continue
        other = records.get(rec.source_id)
        gist = _sentence(getattr(other, "content", "") or "")[:200] if other else ""
        verb = "supports" if rec.type == "supports" else "contradicts"
        lead = f"Given {rec.source_id}" + (f' — "{gist}" — ' if gist else " ")
        out.append(f"{lead}this record is **{verb[:-1]}ed** ({rec.id}, verified).")
    return out


def describe(rid: str, records: Mapping[str, Any], status: Mapping[str, str], flags: Flags) -> list[str]:
    """Everything the archive holds about one cited record, as prose. Absences are stated."""
    rec = records.get(rid)
    kind = KIND.get(rid.split("-")[0], "Record")
    if rec is None:
        return [
            f"**{rid}** — {kind}. **Not present in this knowledge base.** The proposal cites an "
            f"identifier the archive does not hold; the run's own gate would have failed on it."
        ]

    lines = [f"**{rid}** — {kind}."]
    body = (
        getattr(rec, "content", None)
        or getattr(rec, "statement", None)
        or getattr(rec, "title", None)
        or getattr(rec, "name", None)
        or ""
    )
    if body:
        lines.append(f"  {_sentence(body)}")
    else:
        lines.append("  This record carries no statement field.")

    if rid.startswith("C-"):
        st = status.get(rid, "unresolved")
        lines.append(f"  Standing: {STATUS_GLOSS.get(st, st)}.")
        quote = getattr(rec, "quote", None)
        if quote:
            lines.append(f'  It rests on this passage, quoted verbatim from its source: "{_sentence(quote)}"')
        elif getattr(rec, "origin", "") == "computation":
            lines.append("  It is the output of a deterministic analysis, verified by code rather than by a quote.")
        else:
            lines.append("  **No quote is recorded**, so nothing in the archive ties it to a passage.")
        lines.append("  " + _sources(rec))
        lines += ["  " + s for s in _relation_sentences(rid, records, status)]
    elif rid.startswith("EXP-"):
        lines.append(
            f"  Cost {getattr(rec, 'cost', '?')}, feasibility {getattr(rec, 'feasibility', '?')}; "
            f"it tests {', '.join(getattr(rec, 'tested_claims', []) or ['no declared claim'])}."
        )
        outcomes = getattr(rec, "expected_outcomes", None) or []
        if outcomes:
            lines.append(
                f"  It declares {len(outcomes)} expected outcome(s), at least one of which would "
                "contradict the claim it tests — the schema refuses a proposal without one."
            )
    elif rid.startswith("INT-"):
        lines.append(
            f"  Modality {getattr(rec, 'modality', '?')}, target {getattr(rec, 'target', '?')}. "
            f"It depends on {', '.join(getattr(rec, 'acts_on_claims', []) or ['no declared claim'])}."
        )
        for field, label in (
            ("stratification", "Patient stratification"),
            ("biomarker", "Biomarker"),
            ("off_target_risks", "Off-target risks"),
        ):
            value = getattr(rec, field, None)
            if value:
                lines.append(f"  {label}: {_sentence(value if isinstance(value, str) else '; '.join(value))}")
    elif rid.startswith("D-"):
        lines.append(
            f"  Decision: {getattr(rec, 'decision', '?')} on {getattr(rec, 'hypothesis_id', '?')}, "
            f"covering {', '.join(getattr(rec, 'experiment_ids', []) or ['no experiment'])}."
        )

    warn = []
    if rid in flags.contradicted_or_contested:
        warn.append("the run's gate flagged this citation because the claim is contradicted or contested")
    if rid in flags.judged_unfaithful:
        warn.append("this run's own verifier judged the claim to say more than its quote supports")
    if rid in flags.quoting_unnamed_source:
        warn.append("the claim carries a quote but names no source, so it cannot be looked up")
    if warn:
        lines.append("  ⚠ " + "; ".join(warn) + ".")
    return lines


def hypothesis_graph(records: Mapping[str, Any], run_id: str) -> tuple[str, int] | None:
    """The run's hypotheses and their direct neighbours of the graph kinds (claims, experiments, interventions, decisions).

    Only one hop from a hypothesis is kept: claims that feed no hypothesis are left to the knowledge-base graph, so
    the picture stays readable. Returns the Mermaid text and the number of edges the builder dropped because their
    other end belongs to an earlier run. `None` when the run has no hypothesis, so the section is omitted.
    """
    graph = build(records, run_id=run_id)
    by_id = {n["id"]: n for n in graph["nodes"]}
    hypotheses = {n["id"] for n in graph["nodes"] if n["kind"] == "hypothesis"}
    if not hypotheses:
        return None
    neighbours = {
        e["target"] if e["source"] in hypotheses else e["source"]
        for e in graph["edges"]
        if e["source"] in hypotheses or e["target"] in hypotheses
    }
    kept = hypotheses | {i for i in neighbours if i in by_id and by_id[i]["kind"] in GRAPH_KINDS}
    nodes = [by_id[i] for i in by_id if i in kept]
    edges = [e for e in graph["edges"] if e["source"] in kept and e["target"] in kept]
    return to_mermaid({"nodes": nodes, "edges": edges}), graph["dropped_edges"]


def render(run_id: str, kb_root: Path, archive: Path) -> str:
    proposal_path = archive / "final-proposal.json"
    if not proposal_path.exists():
        raise ProposalMissing(f"{run_id} has no final-proposal.json: it did not reach synthesis")
    data = json.loads(proposal_path.read_text())
    proposal, gate = data["proposal"], data.get("gate") or {}
    flags = Flags.read(gate)
    records = read_records(kb_root)
    status = statuses(records)

    sections = [
        ("summary", "Summary"),
        ("mechanism", "A. Disease mechanism"),
        ("therapeutic_hypothesis", "B. Therapeutic hypothesis"),
        ("intervention", "C. Intervention"),
        ("stratification", "D. Patient stratification"),
        ("biomarker", "E. Biomarker"),
        ("validation", "F. Validation and falsification experiments"),
        ("translation", "G. Preclinical-to-clinical path"),
    ]

    cited: list[str] = []
    out = [
        f"# Final research proposal — {run_id}",
        "",
        f"**Outcome:** `{proposal.get('outcome')}`. "
        f"**Proposal gate:** {'passed' if gate.get('passed') else 'failed'}.",
        "",
        "> Generated by a multi-agent system. Hypotheses are hypotheses. Only experiments named as performed",
        "> below carry a verified computational receipt; every other experiment is a proposal. Nothing here is",
        "> medical advice.",
        "",
        "This is the archived proposal with its citation tags resolved. **The prose is exactly as the system",
        "wrote it**; the only additions are the numbered markers, the hypothesis graph and the evidence section",
        "at the end, all produced deterministically from the knowledge base by `evaluation/proposal_readable.py`.",
        "No model was called to write any part of this document.",
        "",
    ]

    def body_of(value: Any) -> str:
        """A section is either a string or `{text, cites}`; `cites` is the host's own parse of the tags."""
        if isinstance(value, Mapping):
            return value.get("text") or ""
        return value or ""

    for key, title in sections:
        text = body_of(proposal.get(key))
        if not text:
            continue

        def mark(match: re.Match[str]) -> str:
            rid = match.group(1)
            if rid not in cited:
                cited.append(rid)
            return f"[{cited.index(rid) + 1}]"

        out += [f"## {title}", "", TAG.sub(mark, text).strip(), ""]

    modes = proposal.get("failure_modes") or []
    if modes:
        out += ["## H. Failure modes", ""]
        for i, mode in enumerate(modes, 1):
            text = body_of(mode)

            def mark(match: re.Match[str]) -> str:
                rid = match.group(1)
                if rid not in cited:
                    cited.append(rid)
                return f"[{cited.index(rid) + 1}]"

            out += [f"{i}. {TAG.sub(mark, text).strip()}"]
        out += [""]

    graph = hypothesis_graph(records, run_id)
    if graph is not None:
        mermaid, dropped = graph
        out += [
            "## Hypothesis graph",
            "",
            GRAPH_CAVEAT,
            "",
            GRAPH_ID_NOTE,
            "",
            "Legend:",
            "",
            *GRAPH_LEGEND,
            "",
            "```mermaid",
            mermaid.rstrip(),
            "```",
            "",
        ]
        if dropped:
            out += [
                f"{dropped} link(s) to records of earlier runs are not drawn: their other end is outside this run.",
                "",
            ]

    out += [
        "## Causal chains",
        "",
        *CHAINS_NOTE,
        *causal_chains(records, run_id, proposal),
    ]

    out += [
        "---",
        "",
        "## Evidence cited",
        "",
        f"Every citation above, in the order it first appears: {len(cited)} records. Each entry states what",
        "the record says, what it rests on, and what the system's own checks concluded about it. A ⚠ marks a",
        "warning the run's proposal gate raised — warnings do not fail a run, by design.",
        "",
    ]
    for i, rid in enumerate(cited, 1):
        lines = describe(rid, records, status, flags)
        out += [f"{i}. " + lines[0]] + lines[1:] + [""]

    if not cited:
        out += ["This proposal cites no record. That is itself a finding and is reported as one.", ""]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("run_id", nargs="+", help="run ids with an archived final proposal")
    parser.add_argument("--kb", type=Path, default=REPO_ROOT / "knowledgebase")
    parser.add_argument("--experiments", type=Path, default=REPO_ROOT / "experiments")
    parser.add_argument("--out", type=Path, default=None, help="default: <experiments>/final-proposals")
    args = parser.parse_args(argv)

    out_dir = args.out or (args.experiments / "final-proposals")
    out_dir.mkdir(parents=True, exist_ok=True)
    failed = 0
    for run_id in args.run_id:
        try:
            text = render(run_id, args.kb, args.experiments / run_id / "results")
        except (ProposalMissing, FileNotFoundError) as exc:
            print(f"{run_id}: skipped — {exc}")
            failed += 1
            continue
        path = out_dir / f"{run_id}.md"
        path.write_text(text)
        print(f"{run_id}: {path} ({len(text.splitlines())} lines)")
    return 1 if failed and failed == len(args.run_id) else 0


if __name__ == "__main__":
    raise SystemExit(main())
