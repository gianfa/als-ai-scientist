"""Gate and rendering of the final proposal (outputs A-H), from the `synthesize` phase.

The proposal is an artefact of the run, not a KB record. The gate checks it against the KB and records what it
finds next to it; it does not rewrite the agent's text:

- every cited ID (in `cites` and in `[[ID]]` tags) must exist in the KB: unknown IDs are errors;
- a claim cited outside the failure-mode section whose computed status is `contradicted` or `contested` is
  listed, because the PI must not override contradictory evidence: the reader sees it next to the text;
- an outcome `proposal` with no `buy` decision in the run is listed: nothing was judged worth testing.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from knowledge_base.chains import CHAINS_NOTE
from knowledge_base.schemas import Claim, Decision
from knowledge_base.status import narrowings, statuses
from knowledge_base.validate import Record
from tools.literature.store import unresolved_refs

from .proposals import Section, SynthesizeOutput
from .section_gates import check_sections

TAG = re.compile(r"\[\[((?:C|H|I|EXP|D|INT)-\d+)\]\]")
ANY_TAG = re.compile(r"\[\[([^]]*)\]\]")  # any bracketed tag, well formed or not
ID = re.compile(r"(?:C|L|N|H|I|V|EXP|D|INT)-\d+")


def normalize_tags(text: str) -> tuple[str, list[str]]:
    """Rewrite each citation tag into one well-formed tag per ID, and report what was rewritten.

    The format is one ID per tag (`[[C-1]] [[C-2]]`), which is what `TAG` and therefore the gate read. A PI that
    writes `[[C-1, C-2]]` or `[[C-9 (contested)]]` makes a citation the gate finds no tag in; rewriting it keeps the
    IDs it named and makes them checkable. Nothing is invented: a tag naming no ID is left untouched, for the gate
    to fail on.
    """
    rewrites: list[str] = []

    def one(match: re.Match[str]) -> str:
        inner = match.group(1)
        ids = list(dict.fromkeys(ID.findall(inner)))
        if not ids or (len(ids) == 1 and inner.strip() == ids[0]):
            return match.group(0)  # already well formed, or nothing to salvage
        rewrites.append(f"{match.group(0)} -> {' '.join(f'[[{i}]]' for i in ids)}")
        return " ".join(f"[[{i}]]" for i in ids)

    return ANY_TAG.sub(one, text), rewrites


def normalize(output: SynthesizeOutput) -> dict[str, list[str]]:
    """Normalize every section's tags in place; returns the rewrites per section, for the phase report."""
    done: dict[str, list[str]] = {}
    for name in SECTIONS:
        section = getattr(output, name)
        section.text, rewrites = normalize_tags(section.text)
        if rewrites:
            done[name] = rewrites
    for n, section in enumerate(output.failure_modes, 1):
        section.text, rewrites = normalize_tags(section.text)
        if rewrites:
            done[f"failure_mode_{n}"] = rewrites
    return done


def malformed(text: str) -> list[str]:
    """Tags the gate cannot read: a bracketed tag that is not exactly one valid record ID."""
    return [m.group(0) for m in ANY_TAG.finditer(text) if not TAG.fullmatch(m.group(0))]


SECTIONS = (
    "mechanism",
    "therapeutic_hypothesis",
    "intervention",
    "stratification",
    "biomarker",
    "validation",
    "translation",
)
TITLES = {
    "mechanism": "A. Disease mechanism",
    "therapeutic_hypothesis": "B. Therapeutic hypothesis",
    "intervention": "C. Intervention",
    "stratification": "D. Patient stratification",
    "biomarker": "E. Biomarker",
    "validation": "F. Validation / falsification experiments",
    "translation": "G. Preclinical-to-clinical translation",
}


def _cited(section: Section) -> list[str]:
    return list(dict.fromkeys([*section.cites, *TAG.findall(section.text)]))


def unfaithful_sources(
    verdicts: Mapping[str, Mapping[str, Any]], records: Mapping[str, Record]
) -> dict[str, list[str]]:
    """Claims the run's own verifier judged to say more than their quote, by claim ID.

    `faithful` is the verifier answering "does the source claim's content say what its quote says, **and no
    more**?" — the question a citation audit asks. Elsewhere the answer only decides whether a confirmed relation is
    copied as verified or narrowed first, and for a `rejected` or `insufficient` verdict it is recorded nowhere,
    so a run could know that a cited claim overstates its source while the proposal says nothing.

    It warns, it never rejects: the verifier's flag is not always right (a claim it called unfaithful can be judged
    sound by a human reader, and the share of `faithful` verdicts varies widely across verifier agents), and a gate
    that refused on it would block citations that hold. The gate does not retry, so one verifier's flag would fail
    the run; when the claim has been narrowed, the entry names the narrowed claim to cite.
    """
    flagged: dict[str, list[str]] = {}
    for relation_id, verdict in verdicts.items():
        if not isinstance(verdict, Mapping) or verdict.get("faithful") is not False:
            continue
        relation = records.get(relation_id)
        source = getattr(relation, "source_id", None)
        if isinstance(records.get(source), Claim):
            flagged.setdefault(str(source), []).append(f"{relation_id} ({verdict.get('verdict')})")
    return flagged


def quotes_an_unnamed_source(claim: Claim) -> bool:
    """A claim carrying a quote that names no source. Unattributable by construction: nobody can check it, now
    or later, because there is no paper to look in.

    It is the cheapest anomaly in this system to detect — record structure, no agent, no network — and nothing
    else reports it: `check_quote` is never called for such a claim, so the run learns nothing, and
    `provenance_status: unverified` collapses it with "the paper is missing" and "the quote was not found". A
    proposal can therefore state every weakness its tools named and stay silent on exactly these claims.
    """
    return bool(claim.quote) and not [r for r in claim.source_refs if not r.startswith("receipt:")]


def executed_experiment_ids(execution: Mapping[str, Any] | None) -> set[str]:
    """The EXPs of this run that hold a verified computational receipt — the only ones a proposal may call
    performed, however many the run executed (`Orchestrator._execute_phase`)."""
    if (execution or {}).get("status") != "executed":
        return set()
    return {
        row["experiment_id"] for row in (execution or {}).get("executions") or [] if row.get("status") == "executed"
    }


def gate(
    output: SynthesizeOutput,
    records: Mapping[str, Record],
    run_id: str,
    verdicts: Mapping[str, Mapping[str, Any]] | None = None,
    execution: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    status = statuses(records)
    unfaithful = unfaithful_sources(verdicts or {}, records)
    unknown: dict[str, list[str]] = {}
    against: dict[str, list[str]] = {}
    bad: dict[str, list[str]] = {}
    for name in SECTIONS:
        section = getattr(output, name)
        if left := malformed(section.text):
            bad[name] = left
        ids = _cited(getattr(output, name))
        missing = [i for i in ids if i not in records]
        if missing:
            unknown[name] = missing
        flagged = [i for i in ids if isinstance(records.get(i), Claim) and status[i] in ("contradicted", "contested")]
        if flagged:
            against[name] = [f"{i} ({status[i]})" for i in flagged]
    for n, section in enumerate(output.failure_modes, 1):
        if left := malformed(section.text):
            bad[f"failure_mode_{n}"] = left
        missing = [i for i in _cited(section) if i not in records]
        if missing:
            unknown[f"failure_mode_{n}"] = missing
    bought = [r.id for r in records.values() if isinstance(r, Decision) and r.run_id == run_id and r.decision == "buy"]
    # Whether the proposal rests on anything the code verified. The gate reports it rather than leaving it to be
    # counted by hand.
    cited = {i for name in SECTIONS for i in _cited(getattr(output, name))}
    cited |= {i for section in output.failure_modes for i in _cited(section)}
    computed = sorted(i for i in cited if isinstance(records.get(i), Claim) and records[i].origin == "computation")
    # The same shape as `cites_contradicted_or_contested`: a citation the run's own verifier had already
    # questioned, named where it is cited. A warning, not a verdict (see `unfaithful_sources`).
    narrowed = narrowings(records)

    def why(i: str) -> str:
        to = f"; narrowed to {', '.join(narrowed[i])}" if i in narrowed else ""
        return f"{i} (judged unfaithful: {', '.join(unfaithful[i])}{to})"

    overstated = {name: [why(i) for i in _cited(getattr(output, name)) if i in unfaithful] for name in SECTIONS}
    overstated = {name: ids for name, ids in overstated.items() if ids}
    unattributable = {
        name: [
            i
            for i in _cited(getattr(output, name))
            if isinstance(records.get(i), Claim) and quotes_an_unnamed_source(records[i])
        ]
        for name in SECTIONS
    }
    unattributable = {name: ids for name, ids in unattributable.items() if ids}
    # A cited claim whose source reference names neither a published identifier nor an artifact of this
    # system -- the literal strings `Web search results`, a claim id, a news domain. Distinct from
    # `cites_quoting_an_unnamed_source`, which is about a quote with no reference at all: here there *is*
    # a reference and it leads nowhere. The host canonicalises what it can and keeps the rest as written,
    # so this names what survived that (`tools.literature.store.unresolved_refs`).
    unresolvable = {
        name: [
            f"{i} ({', '.join(unresolved_refs(records[i].source_refs))})"
            for i in _cited(getattr(output, name))
            if isinstance(records.get(i), Claim) and unresolved_refs(records[i].source_refs)
        ]
        for name in SECTIONS
    }
    unresolvable = {name: ids for name, ids in unresolvable.items() if ids}
    # Every EXP of this run that holds a verified receipt, not the one the selector happened to pick first: a
    # run may execute several, and the gate's rule is the receipt, not the count.
    executed_ids = executed_experiment_ids(execution)
    unsupported_ready = [eid for eid in output.runnable_now_experiment_ids if eid not in executed_ids]
    unsupported_performed = [eid for eid in output.performed_experiment_ids if eid not in executed_ids]
    # B, D, E and G against the records that carry their substance (`section_gates`). Reported, not blocking:
    # the requirements have never been measured, and a requirement that has never been measured must not fail a
    # paid run at its last phase.
    sections_unmet = check_sections(output, records)
    warnings = []
    if output.outcome == "proposal" and not bought:
        warnings.append("outcome is 'proposal' but no decision of this run bought an experiment")
    # A tag the gate cannot read is a citation it cannot check, so it fails the gate rather than passing unseen.
    return {
        "passed": not unknown and not bad and not unsupported_ready and not unsupported_performed,
        "unknown_ids": unknown,
        "malformed_tags": bad,
        "cites_contradicted_or_contested": against,
        "cites_judged_unfaithful": overstated,
        "cites_quoting_an_unnamed_source": unattributable,
        "cites_an_unresolvable_source": unresolvable,
        "runnable_now_without_handler": unsupported_ready,
        "performed_without_receipt": unsupported_performed,
        "sections_not_demonstrated": sections_unmet,
        "warnings": warnings,
        "bought_decisions": bought,
        "computed_claims_cited": computed,
    }


def render(
    output: SynthesizeOutput,
    gate_result: dict[str, Any],
    run_id: str,
    feasibility: Mapping[str, Mapping[str, Any]] | None = None,
    execution: Mapping[str, Any] | None = None,
    reassessment: Mapping[str, Any] | None = None,
    chains: Sequence[str] | None = None,
) -> str:
    executed_ids = executed_experiment_ids(execution)
    performed = bool(executed_ids)
    caveat = (
        "Only experiments named as performed below have a verified computational receipt. Other experiments "
        "are proposals; nothing here is medical advice."
        if performed
        else "Proposed experiments have not been performed, and nothing here is medical advice."
    )
    lines = [
        f"# Final research proposal — {run_id}",
        "",
        f"**Outcome:** {output.outcome}",
        "",
        output.summary,
        "",
        "> Research proposal generated by a multi-agent system. Hypotheses are hypotheses. " + caveat,
        "",
    ]
    for name in SECTIONS:
        lines += [f"## {TITLES[name]}", "", getattr(output, name).text, ""]
    lines += ["## H. Failure modes", ""] + [f"{n}. {s.text}" for n, s in enumerate(output.failure_modes, 1)] + [""]
    if chains is not None:
        # Written by the host from the knowledge base, after the PI's sections: the PI's prose says what it cites, this
        # says what each cited step is worth (`knowledge_base/chains.py`).
        lines += ["## Causal chains", "", *CHAINS_NOTE, *chains]
    exp_ids = sorted(
        (i for i in _cited(output.validation) if i.startswith("EXP-")), key=lambda i: int(i.split("-", 1)[1])
    )
    if exp_ids:
        lines += [
            "## Data feasibility of cited experiments",
            "",
            "These are data checks, not evidence that an analysis was executed.",
            "",
        ]
        for eid in exp_ids:
            row = (feasibility or {}).get(eid)
            status = row["status"] if row else "unverified"
            reason = row["reason"] if row else "no feasibility assessment in this run"
            run_status = "performed (verified receipt)" if eid in executed_ids else "not executed"
            lines.append(f"- {eid}: {status}; {run_status} — {reason}")
        lines.append("")
    judged = [
        entry["judgement"] for entry in (reassessment or {}).get("judgements") or [] if entry.get("status") == "judged"
    ]
    if judged:
        # One subsection per judgement: a run that executed several experiments got several independent
        # verdicts, and merging them would hide a disagreement between them.
        lines += ["## Post-result reassessment", ""]
        for judgement in judged:
            lines += [
                f"### {judgement['experiment_id']}",
                "",
                f"Agent judgement: {judgement['decision']} for {judgement['experiment_id']}.",
                "",
                judgement["rationale"],
                "",
            ]
        lines += ["This is an interpretation of the verified result, not a verified computational fact.", ""]
    lines += ["## Gate", "", f"- passed: {gate_result['passed']}"]
    lines.append(f"- computed claims cited: {', '.join(gate_result['computed_claims_cited']) or 'none'}")
    for key in (
        "unknown_ids",
        "malformed_tags",
        "cites_contradicted_or_contested",
        "cites_judged_unfaithful",
        "cites_quoting_an_unnamed_source",
        "cites_an_unresolvable_source",
    ):
        for section, ids in gate_result.get(key, {}).items():
            lines.append(f"- {key.replace('_', ' ')} in {section}: {', '.join(ids)}")
    lines += [f"- warning: {w}" for w in gate_result["warnings"]]
    if gate_result.get("runnable_now_without_handler"):
        lines.append(
            "- runnable now without verified handler: " + ", ".join(gate_result["runnable_now_without_handler"])
        )
    if gate_result.get("performed_without_receipt"):
        lines.append("- performed without verified receipt: " + ", ".join(gate_result["performed_without_receipt"]))
    return "\n".join(lines) + "\n"
