"""Structural checks on the proposal sections B, D, E and G.

Outputs C, F and H are already demonstrable because something structural is checked: C rests on `Intervention`
records carrying modality, target, off-target risks, a stratification and a biomarker; F refuses an experiment
whose outcomes could not contradict the claim it tests; H counts failure modes against a floor. A, B, D, E and G
were checked only for a non-empty string, so the proposal could satisfy them with prose.

These checks do not read the prose. Each asks whether the section is **traceable to the upstream record that
carries the substance** — which is the system's success criterion, `final claim -> agent artifact ->
evidence ID`. The agents already produce those records; nothing here asks them for anything new.

**A** is checked narrowly. Turning the claim graph into an identified causal model is a
research problem with identification assumptions, not a gate, and checking for it would not make it true. What
*is* mechanical is whether the section states a **path** at all, and whether that path touches any evidence
anybody checked. Both are traceability, like the rest; neither says the mechanism is right.

Each check is a pure function of one section and the knowledge base, independent of the others and of the gate
that calls them. `CHECKS` is the only place a check is registered: adding or dropping one touches one entry and
nothing else. A check returns the requirements the section does **not** meet; an empty list means it meets them.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any

from knowledge_base.schemas import Claim, Experiment, Hypothesis, Intervention
from knowledge_base.status import is_improper, is_proper, mechanism_graph
from knowledge_base.validate import Record

from .proposals import Section

TAG = re.compile(r"\[\[((?:C|H|I|EXP|D|INT)-\d+)\]\]")


def cited_ids(section: Section) -> list[str]:
    """Every ID the section cites, from the `cites` field and from the `[[ID]]` tags in its text."""
    return list(dict.fromkeys(list(section.cites) + TAG.findall(section.text)))


def _cited_of(section: Section, records: Mapping[str, Record], kind: type) -> list[Any]:
    return [records[i] for i in cited_ids(section) if isinstance(records.get(i), kind)]


# --- the four checks, each independent of the others ---------------------------------------------


def check_therapeutic_hypothesis(section: Section, records: Mapping[str, Record]) -> list[str]:
    """B. A therapeutic hypothesis that nothing could contradict is a position, not a hypothesis.

    So the section must name the hypothesis record it states, and at least one experiment that declares an
    outcome contradicting a claim: the falsifier, not merely a test.
    """
    unmet = []
    if not _cited_of(section, records, Hypothesis):
        unmet.append("cites no H- hypothesis record")
    experiments = _cited_of(section, records, Experiment)
    if not experiments:
        unmet.append("cites no EXP- experiment")
    elif not any(outcome.effect == "contradicts" for exp in experiments for outcome in exp.expected_outcomes):
        unmet.append("cites no experiment declaring an outcome that would contradict a claim")
    return unmet


def check_stratification(section: Section, records: Mapping[str, Record]) -> list[str]:
    """D. The subgroup must be traceable to a candidate that states who it is for, and on what criterion.

    `Intervention.stratification` is a mandatory non-empty field upstream, so the substance exists whenever an
    INT- record does. What was missing is the requirement that section D connect to it.
    """
    if not any(getattr(i, "stratification", None) for i in _cited_of(section, records, Intervention)):
        return ["cites no INT- candidate carrying a stratification"]
    return []


def check_biomarker(section: Section, records: Mapping[str, Record]) -> list[str]:
    """E. The same, for what would be measured: `Intervention.biomarker`."""
    if not any(getattr(i, "biomarker", None) for i in _cited_of(section, records, Intervention)):
        return ["cites no INT- candidate carrying a biomarker"]
    return []


def check_translation(section: Section, records: Mapping[str, Record]) -> list[str]:
    """G. A path from preclinical to clinical is a sequence of stages, and a stage is an experiment.

    A translation section that names no experiment describes a route nobody could walk; one that names
    experiments of a single kind describes one stage, not a path. Two kinds is the weakest requirement that
    distinguishes a path from a stage, and it is checked against the records, not against the prose.
    """
    experiments = _cited_of(section, records, Experiment)
    if len(experiments) < 2:
        return ["cites fewer than two EXP- experiments, so it names no sequence of stages"]
    if len({exp.kind for exp in experiments}) < 2:
        return ["cites experiments of a single kind, so it names one stage rather than a path"]
    return []


def check_mechanism(section: Section, records: Mapping[str, Record]) -> list[str]:
    """A. A mechanism is a connected backbone of proper edges plus at most one improper edge (ADR 0033).

    Over the **causal claims** the section cites — those carrying the causal block, which is what makes a claim an
    edge of the mechanism graph (`specs.md`, "causality is a shape, not a type"):

    1. A cited edge that the mechanism view hides does not count and is named: one that other verified claims
       contradict or contest, or one that a newer claim superseded. The knowledge base keeps it; a mechanism does not
       rest on it.
    2. At least one cited edge must be **proper** (`knowledge_base.status.is_proper`): its quote found in its stored
       source, or supported by an active verified relation. A mechanism every step of which is an unchecked agent
       assertion is a story the system told itself.
    3. At most **one** cited edge may be improper: the hypothesis step. A long run of unchecked steps is not admitted.
    4. The proper edges must join: they form one connected set through shared entities, not a list of causes.
    5. The improper edge must be adjacent to the proper ones (it shares an entity with one of them), so that it is a
       step from checked ground and not a separate assertion.

    The rule is a floor, not a standard: it does not say the backbone is long (the knowledge base is asked for the
    longest by `kb_mechanism_path`) nor that it concerns the cited hypothesis. It does not read aliases from
    `knowledge_base/dedup.py`: joins are on entity ids as stored.

    **This rule was written with the archives of fig8 (`pl-prod-014`..`018`) in view**, like its predecessor, so the first
    honest test of it is a run it was not fitted to.
    """
    cited = [c for c in _cited_of(section, records, Claim) if c.causal_fields() is not None]
    if not cited:
        return ["cites no causal claim, so it states no mechanism"]
    view = {e.claim_id: e for e in mechanism_graph(records)}
    shown = [view[c.id] for c in cited if c.id in view]
    hidden = [c.id for c in cited if c.id not in view or not (is_proper(view[c.id]) or is_improper(view[c.id]))]
    unmet: list[str] = []
    if hidden:
        unmet.append(
            f"cites causal claims that the mechanism view hides (contradicted, contested or superseded): "
            f"{', '.join(sorted(hidden))}"
        )
    proper = [e for e in shown if is_proper(e)]
    improper = [e for e in shown if is_improper(e)]
    if not proper:
        unmet.append(
            "cites no proper causal edge (its quote verified against its source, or supported by a verified "
            "relation): every step of the chain is an unchecked assertion"
        )
        return unmet
    if len(improper) > 1:
        unmet.append(
            f"cites {len(improper)} unchecked causal edges ({', '.join(e.claim_id for e in improper)}); "
            "at most one, the hypothesis step, is allowed"
        )
    if not _joined(proper):
        unmet.append("cites proper causal edges that do not join: a list of causes, not a chain")
    nodes = {n for e in proper for n in (e.cause_id, e.effect_id)}
    if len(improper) == 1 and improper[0].cause_id not in nodes and improper[0].effect_id not in nodes:
        unmet.append(f"its unchecked edge {improper[0].claim_id} is not adjacent to the proper chain")
    return unmet


def _joined(edges: list[Any]) -> bool:
    """Whether the edges form one connected set when read as undirected, through their cause and effect ids."""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for e in edges:
        parent[find(e.cause_id)] = find(e.effect_id)
    return len({find(e.cause_id) for e in edges}) <= 1


SectionCheck = Callable[[Section, Mapping[str, Record]], list[str]]

# section name -> (what the section must contain, the check). The only registry.
CHECKS: dict[str, tuple[str, SectionCheck]] = {
    "mechanism": ("A. a disease mechanism stated as a chain that touches checked evidence", check_mechanism),
    "therapeutic_hypothesis": (
        "B. a mechanistic hypothesis with something that could contradict it",
        check_therapeutic_hypothesis,
    ),
    "stratification": ("D. a patient subgroup named on a measurable criterion", check_stratification),
    "biomarker": ("E. a measurable biomarker of biological response", check_biomarker),
    "translation": ("G. an ordered preclinical-to-clinical path", check_translation),
}


def check_sections(output: Any, records: Mapping[str, Record]) -> dict[str, dict[str, Any]]:
    """Run every registered check. Returns one entry per section that fails, naming what it asks and what is missing.

    **Reported, not blocking.** These requirements were written before any archive was
    scored against them, and a requirement that has never been measured must not fail a paid run at its last
    phase. Whether any of them becomes blocking is a decision to take once the archives have been scored, and it
    is recorded as such rather than taken here by default.
    """
    findings: dict[str, dict[str, Any]] = {}
    for name, (requirement, check) in CHECKS.items():
        section = getattr(output, name, None)
        if section is None:
            continue
        if unmet := check(section, records):
            findings[name] = {"requires": requirement, "missing": unmet}
    return findings
