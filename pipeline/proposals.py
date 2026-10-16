"""What agents return, and how the orchestrator turns it into KB records.

Agents never write IDs or statuses. They refer to the records they propose with local `ref`s ("c1", "h1"), to
records already in the KB by their ID (from the slice they were given), and to entities by name. `to_records`
allocates IDs, resolves names and refs, sets provenance through a `Verifier`, and returns records in an order
the store accepts (entities, claims, hypotheses, ignorance, relations).

The JSON Schemas handed to the agents are generated from these models (`agent_schema`), so the JS side holds no
copy that could drift.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError, model_validator

from knowledge_base.schemas import (
    Claim,
    DatasetLocation,
    Decision,
    DecisionType,
    Entity,
    ExpectedOutcome,
    Experiment,
    ExperimentId,
    ExperimentKind,
    Hypothesis,
    Ignorance,
    Intervention,
    Level,
    Modality,
    Relation,
    StatementType,
    canonical_hash,
    entity_key,
)
from knowledge_base.validate import Record
from tools.literature.store import SOURCE_FORM_HELP, canonicalise_refs, is_declared_source, recovered_source

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Ref = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,31}$")]
KbId = Annotated[str, StringConstraints(pattern=r"^C-\d+$")]
# The host compares a reported digest character for character with the verified artifact's, so the field is an
# identifier and not a sentence: an earlier run's reassessor wrote the digest with a parenthesis appended and
# its judgement could not bind.
Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class _Proposal(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ProposedCausal(_Proposal):
    cause: Text = Field(description="Name of the cause entity")
    effect: Text = Field(description="Name of the effect entity")
    context: Text
    mechanism: Text


class ProposedClaim(_Proposal):
    ref: Ref = Field(description="Local reference, unique in this answer (e.g. c1)")
    type: StatementType = Field(
        description="What kind of statement this is, not whether it asserts a cause: "
        "fill `causal` for that, and the two are independent"
    )
    content: Text = Field(description="One atomic statement")
    source: Text | None = Field(
        None,
        description="Where the claim comes from: one external reference, and "
        "nothing else. Knowledge-base claim IDs belong in "
        "`derived_from`, a conflict in `contradicts`, your reasons "
        "in `reasoning` -- never here. " + SOURCE_FORM_HELP,
    )
    quote: Text | None = Field(None, description="Verbatim sentence from the source that states the claim")
    derived_from: list[Ref | KbId] = Field(
        default_factory=list,
        description="The records you reasoned from: C- IDs from the "
        "knowledge-base slice, or local refs from this answer. "
        "Internal reasoning goes here, not in `source`",
    )
    contradicts: list[Ref | KbId] = Field(
        default_factory=list,
        description="Records this claim conflicts with. Flag the conflict "
        "here; prose in another field does not reach the graph",
    )
    reasoning: Text | None = Field(
        None,
        description="Why those records license this claim, in one sentence. It "
        "becomes the rationale of the relations they produce, so it "
        "needs at least one `derived_from` or `contradicts`",
    )
    entities: list[Text] = Field(default_factory=list, description="Names of the entities the claim is about")
    causal: ProposedCausal | None = Field(
        None,
        description="Fill this whenever the claim asserts that one thing "
        "causes another, whatever its `type`: it makes the "
        "claim an edge of the mechanism graph. Omit it when "
        "the claim asserts no cause",
    )

    @model_validator(mode="after")
    def _source_declares_its_kind(self) -> ProposedClaim:
        """A source must say what kind of identifier it is, or be a full URL.

        The point is **diagnosability**, not purity: a typed reference can be chased later, by hand or by a
        search, while a bare `41034581` or the sentence `Web search results` cannot. Both are real values
        from production runs. Refusing here rather than asking in a prompt is deliberate —
        the agent cannot be relied on to comply, and the structured-output contract can.

        Declaring the kind also retires the canonicaliser's one guess: an untyped five-to-nine-digit number
        is refused rather than read as a PMID.

        A reference whose *shape* already names its kind -- a bare DOI, a PMCID -- is accepted and
        canonicalised by the host (`recovered_source`), because refusing it serves neither purpose above: it
        would cost claims their attribution over a missing four-character prefix.
        """
        if self.source is not None and not is_declared_source(self.source) and recovered_source(self.source) is None:
            raise ValueError(f"source {self.source!r} does not declare its kind. {SOURCE_FORM_HELP}")
        return self

    @model_validator(mode="after")
    def _reasoning_has_a_subject(self) -> ProposedClaim:
        """`reasoning` is carried by the relations `derived_from` and `contradicts` produce, so without one of
        them it has nowhere to be written and would be silently dropped. Refusing says so instead."""
        if self.reasoning is not None and not (self.derived_from or self.contradicts):
            raise ValueError("reasoning without derived_from or contradicts has nowhere to be recorded")
        return self


class ProposedLink(_Proposal):
    type: Literal["supports", "contradicts", "derived_from", "depends_on"]
    source: Ref | KbId = Field(description="A local ref or a C- ID from the knowledge-base slice")
    target: Ref | KbId
    rationale: Text


class ProposedIgnorance(_Proposal):
    content: Text = Field(description="What you do not know and would need to know")
    questions: list[Ref | KbId] = Field(default_factory=list, description="Claims this gap concerns")


class ProposedHypothesis(_Proposal):
    statement: Text
    mechanism: Text
    author_confidence: Annotated[int, Field(ge=0, le=5)]
    claims: list[ProposedClaim] = Field(min_length=1, description="The atomic claims that make up the hypothesis")
    links: list[ProposedLink] = Field(default_factory=list)
    ignorance: list[ProposedIgnorance] = Field(default_factory=list)


class ReviewOutput(_Proposal):
    """One independent reviewer's report on one hypothesis. Objections backed by a quoted source become claims
    linked by `contradicts`; objections that are missing knowledge become `gaps` (never negative evidence)."""

    claims: list[ProposedClaim] = Field(
        default_factory=list, description="Evidence you found, each with source and quote"
    )
    links: list[ProposedLink] = Field(
        default_factory=list, description="From your evidence refs (or KB claim IDs) to the hypothesis claim IDs"
    )
    gaps: list[ProposedIgnorance] = Field(default_factory=list)
    summary: Text = Field(description="Your overall assessment in 2-4 sentences")


class Verdict(_Proposal):
    relation_id: Annotated[str, StringConstraints(pattern=r"^L-\d+$")]
    faithful: bool = Field(description="Does the source claim's content say what its quote says, no more?")
    verdict: Literal["confirmed", "rejected", "insufficient"] = Field(
        description="Does the quoted passage itself support (or contradict) the target claim as the relation states?"
    )
    reason: Text
    narrowed: Text | None = Field(
        default=None,
        description="Only when the verdict is `confirmed` and `faithful` is false: the source claim's content "
        "restated so that it says exactly what the quote says and no more. Remove what the quote "
        "does not state; add nothing, interpret nothing. Omit this field otherwise.",
    )


class VerifyOutput(_Proposal):
    verdicts: list[Verdict]


class ProposedExperiment(_Proposal):
    title: Text
    protocol: Text = Field(description="Model system, intervention, measurements, controls, sample and readout")
    tested_claims: list[KbId] = Field(min_length=1, description="C- IDs of the hypothesis claims it tests")
    expected_outcomes: list[ExpectedOutcome] = Field(
        min_length=2, description="At least one outcome that supports and one that contradicts a tested claim"
    )
    cost: Level
    cost_rationale: Text
    feasibility: Level
    kind: ExperimentKind = Field(
        "wet_lab",
        description="in_silico: computable from a dataset this run can reach, "
        "which this system could run itself; wet_lab: a trial, "
        "cohort or in-vivo study someone else must fund",
    )
    dataset_refs: list[Text] = Field(
        default_factory=list,
        description="For in_silico: the dataset ids from the data tools, or an "
        "accession for data not yet acquired. State what you would "
        "compute, never what the dataset contains",
    )
    dataset_location: DatasetLocation | None = Field(
        None, description="local: already declared and hashed by this " "run; remote: it would have to be acquired"
    )
    required_markers: list[Text] = Field(
        default_factory=list,
        description="Exact marker names the computation needs, from data_describe; "
        "do not substitute related cytokines",
    )
    required_metadata: list[Text] = Field(
        default_factory=list,
        description="Clinical/sample fields needed for the analysis, such as "
        "disease stage or donor mapping. Declare even if unavailable",
    )
    analysis_id: Literal["hla_panel_compact_v1", "ics_cytokine_skew_v1"] | None = Field(
        None,
        description="Only for a registered analysis with exactly its declared local inputs: "
        "hla_panel_compact_v1 (HLA allele-panel coverage diagnostic; not EXP-63's population-coverage "
        "experiment) or ics_cytokine_skew_v1 (CD4+ IFNg / (IL-4 + IL-10) skew, ALS vs HC)",
    )


class DesignOutput(_Proposal):
    experiments: list[ProposedExperiment] = Field(default_factory=list)
    no_experiment_reason: Text | None = Field(None, description="Only if you propose no experiment: why")


class ExecuteOutput(_Proposal):
    experiment_id: ExperimentId
    analysis_id: Literal["hla_panel_compact_v1", "ics_cytokine_skew_v1"]
    result_digest: Digest
    outcome: Literal["panel_condition_met", "panel_condition_failed", "skew_replicated", "skew_not_replicated"]
    interpretation: Literal[
        "supports_panel_condition_only",
        "weakens_panel_condition_only",
        "supports_ifng_skew_only",
        "weakens_ifng_skew_only",
    ]
    limitations: list[Text] = Field(min_length=1)


class ReassessOutput(_Proposal):
    experiment_id: ExperimentId
    result_digest: Digest
    decision: Literal["keep", "weaken", "reject", "inconclusive"]
    rationale: Text
    claim_ids: list[KbId] = Field(min_length=1)
    limitations: list[Text] = Field(min_length=1)


NoveltyVerdict = Literal["anticipated", "partly_anticipated", "no_prior_art_found"]


class ClosestWork(_Proposal):
    openalex_id: Text = Field(
        description="The `openalex_id` of a work a `search_works` call returned, copied "
        "exactly; never one remembered or invented"
    )
    title: Text
    why_close: Text = Field(description="What this work already says that the hypothesis also says, in your words")


class NoveltyOutput(_Proposal):
    hypothesis_id: Annotated[str, StringConstraints(pattern=r"^H-\d+$")]
    verdict: NoveltyVerdict = Field(
        description="anticipated: a work already states the central mechanism; "
        "partly_anticipated: works state parts of it; no_prior_art_found: "
        "none of the works you read does, which is NOT a finding of novelty"
    )
    rationale: Text
    # Required, not defaulted: a defaulted field is optional in the schema the agent is handed, and two of six
    # agents (`pl-prod-013`, H-3 then H-2) named their prior art in `rationale` and left this out.
    closest_works: list[ClosestWork] = Field(
        description="Every work you name in the rationale, with the "
        "`openalex_id` a search returned; an empty list only "
        "for no_prior_art_found"
    )
    queries: list[Text] = Field(min_length=1, description="Every query you ran, as written")
    full_texts_read: list[Text] = Field(
        description="The DOI of each paper whose full text you opened with "
        "`read_paper`; an empty list if you opened none"
    )
    limitations: list[Text] = Field(min_length=1)

    @model_validator(mode="after")
    def _prior_art_is_named(self) -> "NoveltyOutput":
        if self.verdict != "no_prior_art_found" and not self.closest_works:
            raise ValueError("a verdict of prior art must name the works it rests on")
        # A work named in the rationale but missing from `closest_works` is NOT checked here any more. Telling a
        # formatting lapse (the work was served, the agent just did not list it) from a fabrication needs the
        # search cache, which this model does not have, and one raise for both facts cost a whole assessment:
        # four probes on `pl-prod-013` each lost a hypothesis that way. The host partitions instead, per work:
        # `_work_provenance` in `pipeline/orchestrator.py`.
        return self


class ProposedIntervention(_Proposal):
    name: Text
    modality: Modality
    target: Text = Field(description="The molecule, cell population or process the candidate acts upon")
    acts_on_claims: list[KbId] = Field(min_length=1, description="C- IDs of the mechanistic steps it assumes")
    rationale: Text
    predicted_effect: Text = Field(description="What should change if the mechanism holds, stated so it could fail")
    off_target_risks: list[Text] = Field(min_length=1, description="At least one; silence is not safety")
    safety_concerns: list[Text] = Field(min_length=1, description="At least one; silence is not safety")
    stratification: Text = Field(description="Which patients, on a measurable criterion")
    biomarker: Text = Field(description="What to measure to tell whether it works")
    maturity: Level = Field(description="How far this modality is from the clinic")
    drug_name: Text | None = Field(
        None,
        description="Name of a real, existing compound, only if you are sure of it; "
        "never a guess, and leave empty for a novel candidate",
    )


class InterveneOutput(_Proposal):
    candidates: list[ProposedIntervention] = Field(default_factory=list)
    no_candidate_reason: Text | None = Field(None, description="Only if you propose no candidate: why")


class ArbitrateOutput(_Proposal):
    decision: DecisionType = Field(
        description="buy: run the named experiments; rejected: the hypothesis is "
        "contradicted or not worth testing; stop: neither, for now"
    )
    experiment_ids: list[Annotated[str, StringConstraints(pattern=r"^EXP-\d+$")]] = Field(default_factory=list)
    value_of_information: Level
    rationale: Text


class Section(_Proposal):
    text: Text = Field(description="Cite KB records as [[C-12]], [[H-3]], [[EXP-2]]")
    cites: list[Annotated[str, StringConstraints(pattern=r"^(C|H|I|EXP|D|INT)-\d+$")]] = Field(default_factory=list)


class SynthesizeOutput(_Proposal):
    outcome: Literal["proposal", "insufficient_evidence"]
    summary: Text
    runnable_now_experiment_ids: list[Annotated[str, StringConstraints(pattern=r"^EXP-\d+$")]] = Field(
        default_factory=list,
        description="Only EXP IDs with both data support and an implemented, verified "
        "execution path. An unexecuted proposal is not runnable now",
    )
    performed_experiment_ids: list[Annotated[str, StringConstraints(pattern=r"^EXP-\d+$")]] = Field(
        default_factory=list, description="Only EXP IDs with a verified execution receipt in this run"
    )
    mechanism: Section = Field(description="A. Causal model of the C9orf72-immune-neurodegeneration pathway")
    therapeutic_hypothesis: Section = Field(description="B. Precise mechanistic hypothesis")
    intervention: Section = Field(description="C. Proposed intervention, or why none is supported")
    stratification: Section = Field(description="D. Patient subgroup most likely to benefit")
    biomarker: Section = Field(description="E. Measurable biomarker of biological response")
    validation: Section = Field(description="F. Experiments capable of falsifying the hypothesis (EXP- IDs)")
    translation: Section = Field(description="G. Preclinical-to-clinical path")
    failure_modes: list[Section] = Field(min_length=5, description="H. At least five reasons it might fail")


class LiteratureOutput(_Proposal):
    claims: list[ProposedClaim]


class GenerateOutput(_Proposal):
    hypothesis: ProposedHypothesis


KB_CLAIM_ID = re.compile(r"\bC-\d+\b")
# `CONTRADICTS C-879, …`: the word only counts as a marker when claim IDs follow it immediately, so prose that
# merely contains the verb ("which contradicts the Th2 account") is not read as a flagged conflict.
CONTRADICTS_MARKER = re.compile(r"\bcontradicts\b\s*:?\s*(?=C-\d+)", re.IGNORECASE)


def _as_internal_reasoning(raw: dict[str, Any]) -> tuple[dict[str, Any], str] | None:
    """A `source` that names knowledge-base claims instead of a reference, moved into the typed fields.

    Hypothesis authors often put in `source` not a citation but what they reasoned from -- `'C-232, C-505 (causal); C9orf72 loss of function is the pathophysiological
    consequence of repeat expansion'` -- and some also flagging a conflict it had noticed
    (`'… CONTRADICTS C-879, C-935, C-940 (unresolved contradiction)'`). The field is typed for one external
    reference, so the host would refuse it correctly and discard the reasoning with it.

    It is kept here instead: the IDs before any `CONTRADICTS` marker become `derived_from`, those after it
    `contradicts`, and the author's own sentence becomes the `reasoning` those relations carry -- but only when
    removing the IDs leaves a sentence, so a bare `'C-269'` yields no rationale saying nothing. Returns the
    repaired claim and what was done to it, or `None` when the value names no claim and this is not its case.
    """
    source = raw.get("source")
    if not isinstance(source, str) or not KB_CLAIM_ID.search(source):
        return None
    marker = CONTRADICTS_MARKER.search(source)
    head, tail = (source[: marker.start()], source[marker.end() :]) if marker else (source, "")

    def ids(text: str) -> list[str]:
        return list(dict.fromkeys(KB_CLAIM_ID.findall(text)))

    derived = [i for i in ids(head) if i not in (raw.get("derived_from") or [])]
    conflicts = [i for i in ids(tail) if i not in (raw.get("contradicts") or [])]
    prose = re.search(r"[A-Za-z]{3,}", KB_CLAIM_ID.sub("", source)) is not None
    repaired = {
        **raw,
        "source": None,
        "derived_from": [*(raw.get("derived_from") or []), *derived],
        "contradicts": [*(raw.get("contradicts") or []), *conflicts],
        "reasoning": raw.get("reasoning") or (source if prose else None),
    }
    routed = ", ".join(
        part
        for part in (
            f"derived_from {' '.join(derived)}" if derived else "",
            f"contradicts {' '.join(conflicts)}" if conflicts else "",
        )
        if part
    )
    return repaired, (
        f"source {source!r} names knowledge-base claims, not a reference; routed to {routed}"
        + (", with the author's text kept as their rationale" if prose else "")
    )


def _without_orphan_reasoning(raw: dict[str, Any]) -> tuple[dict[str, Any], str] | None:
    """`reasoning` with no `derived_from` and no `contradicts`: the sentence goes, the claim stays.

    The schema refuses that combination, and refusing is right -- the relation that would carry the sentence does
    not exist, so keeping it would drop it in silence. But letting the refusal reach the claim unrepaired would
    discard many literature claims, and the relations pointing at them, whenever agents fill `reasoning` on
    claims that derive from nothing. The content is not what is faulty, and the same argument as for an
    undeclared source applies with more force, because here the field is one this system added.
    """
    if raw.get("reasoning") is None or raw.get("derived_from") or raw.get("contradicts"):
        return None
    return (
        {**raw, "reasoning": None},
        f"reasoning {raw['reasoning']!r} has no derived_from or contradicts to carry it; the sentence is "
        "dropped and the claim kept",
    )


def _without_source(raw: dict[str, Any]) -> tuple[dict[str, Any], str] | None:
    """A source that declares no kind and names no record: the attribution goes, the claim stays.

    The builder then records it as `origin: agent` -- the system's own reasoning rather than literature.
    Measured over the archive, 12% of stored literature claims name no usable identifier, and losing one claim in
    eight to a provenance rule the agents were never told about would be a worse failure than the one being fixed.
    """
    if raw.get("source") is None:
        return None
    return (
        {**raw, "source": None},
        f"source {raw['source']!r} does not declare its kind; kept as the system's own reasoning, " "with no source",
    )


# Applied in order, cheapest loss first, and **only while the claim is still invalid**: a claim whose one fault
# is an orphan `reasoning` must not also lose a perfectly good DOI. A claim with two faults is therefore
# repaired twice rather than dropped for the second, and one with a single fault is repaired once.
REPAIRS = (_as_internal_reasoning, _without_orphan_reasoning, _without_source)


def _valid(raw: dict[str, Any]) -> bool:
    try:
        ProposedClaim.model_validate(raw)
    except ValidationError:
        return False
    return True


def drop_invalid_claims(output: Any) -> tuple[Any, list[str], list[str]]:
    """Clean an agent's raw output so one faulty claim does not cost the whole of it.

    Returns the cleaned output, the claims dropped with their reason, and the claims **repaired** with
    what was done to them. Links pointing at a dropped claim are then skipped by the builder.

    `REPAIRS` holds them, in order, and they are the reason this function returns a third list. Each is a pure
    transform; the result is validated **once**, after all of them, so a claim with two faults is repaired twice
    instead of being dropped for the second. What they share is the principle: a provenance field is wrong, the
    claim's content is not, and dropping the content is the larger loss.
    """
    if not isinstance(output, dict):
        return output, [], []
    reasons: list[str] = []
    repairs: list[str] = []

    def clean(claims: Any) -> Any:
        if not isinstance(claims, list):
            return claims
        kept = []
        for raw in claims:
            try:
                ProposedClaim.model_validate(raw)
                kept.append(raw)
                continue
            except ValidationError as err:
                ref = raw.get("ref") if isinstance(raw, dict) else None
                first = err.errors()[0]["msg"]
            if isinstance(raw, dict):
                fixed, notes = raw, []
                for repair in REPAIRS:
                    outcome = repair(fixed)
                    if outcome is None:
                        continue
                    fixed, note = outcome
                    notes.append(note)
                    if _valid(fixed):
                        break
                if notes and _valid(fixed):
                    kept.append(fixed)
                    repairs.extend(f"claim {ref}: {note}" for note in notes)
                    continue
            reasons.append(f"claim {ref}: {first}")
        return kept

    output = dict(output)
    if "claims" in output:
        output["claims"] = clean(output["claims"])
    if isinstance(output.get("hypothesis"), dict) and "claims" in output["hypothesis"]:
        output["hypothesis"] = {**output["hypothesis"], "claims": clean(output["hypothesis"]["claims"])}
    return output, reasons, repairs


def agent_schema(model: type[BaseModel]) -> dict[str, Any]:
    """The model's JSON Schema with every `$ref` inlined (no `$defs`), as passed to `agent({schema})`."""
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})

    # Keys whose values are a map from a *name* to a sub-schema: their keys are not schema keywords, so
    # a property legitimately named "title" must survive the metadata strip below.
    named_subschemas = ("properties", "patternProperties")

    def inline(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                return inline(defs[node["$ref"].rsplit("/", 1)[1]])
            out: dict[str, Any] = {}
            for key, value in node.items():
                if key == "title":  # Pydantic's cosmetic schema title, not a property
                    continue
                if key in named_subschemas and isinstance(value, dict):
                    out[key] = {name: inline(sub) for name, sub in value.items()}
                else:
                    out[key] = inline(value)
            return out
        if isinstance(node, list):
            return [inline(v) for v in node]
        return node

    return inline(schema)


# --- conversion -----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Provenance:
    status: Literal["verified", "unverified"]
    method: Literal["quote", "none"]
    refs: tuple[str, ...] = ()


class Verifier(Protocol):
    def __call__(self, claim: ProposedClaim) -> Provenance: ...


def unverified(_: ProposedClaim) -> Provenance:
    return Provenance("unverified", "none")


@dataclass
class Batch:
    records: list[Record] = field(default_factory=list)
    refs: dict[str, str] = field(default_factory=dict)  # local ref -> allocated ID
    skipped: list[str] = field(default_factory=list)  # elements dropped, with the reason
    converted: list[str] = field(default_factory=list)  # elements kept in another form, with the reason
    unresolved: list[dict[str, Any]] = field(default_factory=list)  # claims whose source leads nowhere


class Builder:
    """Turns proposals into records against the current KB. `allocate` is `KnowledgeBase.allocate`."""

    def __init__(
        self,
        known: Mapping[str, Record],
        allocate: Callable[[str], str],
        run_id: str,
        agent_id: str,
        now: datetime,
        verifier: Verifier = unverified,
    ) -> None:
        self.known, self.allocate, self.run_id, self.agent_id, self.now = known, allocate, run_id, agent_id, now
        self.verifier = verifier
        self.batch = Batch()
        self.hypothesis_id: str | None = None  # the hypothesis the current output is about, when there is one
        self._derivations: list[tuple[str, ProposedClaim]] = []  # internal reasoning, flushed once the claims exist
        self._entities = {
            entity_key(n): r.id for r in known.values() if isinstance(r, Entity) for n in (r.canonical_name, *r.aliases)
        }

    def _stamp(self) -> dict[str, Any]:
        return {"run_id": self.run_id, "created_at": self.now}

    def entity(self, name: str) -> str:
        """The entity an agent's name refers to, created only when no existing one matches.

        Matching uses `entity_key`, not `normalize`: the latter feeds `canonical_hash` and changing it would
        change the identity of every claim already stored. `entity_key` merges the spellings agents vary
        freely — case, plural, hyphen, underscore, a trailing parenthetical — and merges no meanings, so
        `IL-10 responses` and `IL-10-mediated T cell responses` remain two entities. That half of the
        fragmentation is not a string problem and is not solved here.

        Entities already written stay as they are — the store is append-only — so this changes what later runs
        write, not what is already written.
        """
        key = entity_key(name)
        if key not in self._entities:
            eid = self.allocate("entity")
            self._entities[key] = eid
            self.batch.records.append(Entity(id=eid, canonical_name=name, **self._stamp()))
        return self._entities[key]

    def claim(self, proposed: ProposedClaim, origin: str) -> str:
        if proposed.ref in self.batch.refs:
            raise ValueError(f"duplicate local ref {proposed.ref}")
        provenance = self.verifier(proposed)
        causal: dict[str, Any] = {}
        if proposed.causal is not None:
            c = proposed.causal
            causal = {
                "cause_ref": self.entity(c.cause),
                "effect_ref": self.entity(c.effect),
                "context": c.context,
                "mechanism": c.mechanism,
            }
        # The agent writes a source reference however it likes -- a bare DOI, `PMID:123`, `PMC456`, or a
        # sentence -- and nothing downstream could join them. The host is the single writer, so it is the
        # host that puts them into one form; the agent is neither asked nor trusted to. What cannot be
        # resolved is kept exactly as written rather than dropped, because a claim resting on nothing
        # checkable must stay visible (`unresolved_refs`, reported by the proposal gate).
        # Defence in depth behind the schema validator: a source that neither declares its kind nor
        # identifies itself by shape cannot be looked up by anyone, so the claim is recorded as the
        # system's own reasoning rather than as literature, and the string the agent wrote is kept
        # rather than discarded. A bare DOI or PMCID is recognised instead of refused, and the recognition
        # is reported -- the host recognising is not the agent declaring, and the record says which it was.
        declared = bool(proposed.source) and is_declared_source(proposed.source)
        recovered = None if declared else recovered_source(proposed.source)
        if recovered is not None:
            self.batch.converted.append(
                f"claim {proposed.ref}: source {proposed.source!r} does not declare its kind but identifies "
                f"itself; recognised by the host as {recovered} and kept as literature"
            )
        elif proposed.source and not declared:
            origin = "agent"
        written = [proposed.source] if declared or recovered else []
        refs, unresolved = canonicalise_refs([*written, *provenance.refs])
        cid = self.allocate("claim")
        # `is_declared_source` is a shape test — it says a reference can be chased, not that it leads anywhere
        # — so a news URL is a well-formed `url:` source and is kept as literature. Whether it names a
        # published source is the *second* value `canonicalise_refs` returns, and previously it was
        # discarded here and recomputed twelve phases later by `synthesis.gate`. an earlier run's `C-87` cited
        # a university press page in three sections of the final proposal before anything said so (bug
        # observatory entry 11). The reference is still kept, and the claim is still written: a claim resting
        # on nothing checkable must stay visible. What changes is that the phase that wrote it says so.
        if unresolved:
            self.batch.unresolved.append({"claim_id": cid, "ref": proposed.ref, "unresolved_source_refs": unresolved})
        hash_fields = {k: str(v) for k, v in causal.items()} if causal else None
        self.batch.records.append(
            Claim(
                id=cid,
                canonical_hash=canonical_hash(proposed.content, hash_fields),
                type=proposed.type,
                content=proposed.content,
                origin=origin,
                provenance_status=provenance.status,
                verification_method=provenance.method,
                source_refs=refs,
                source_refs_raw=(
                    [r for r in written if r not in refs]
                    or ([proposed.source] if proposed.source and not declared and not recovered else [])
                ),
                entity_refs=list(dict.fromkeys(self.entity(n) for n in proposed.entities)),
                quote=proposed.quote,
                **causal,
                **self._stamp(),
            )
        )
        self.batch.refs[proposed.ref] = cid
        if proposed.derived_from or proposed.contradicts:
            self._derivations.append((cid, proposed))
        return cid

    def _flush_derivations(self) -> None:
        """The reasoning an author cites internally, written as relations once every claim of the batch exists.

        Deferred rather than written inside `claim`, for two reasons: a local ref may name a claim later in the
        same answer, and a `contradicts` that has to become a question needs the hypothesis the question hangs
        from (`_as_question`), which is allocated after the claims.

        A target that is not a claim is skipped with its reason rather than written: `derived_from` and
        `contradicts` both run between claims (`validate.ENDPOINTS`), and a relation onto a record the store
        does not hold would fail the whole batch instead of this one element.
        """
        for cid, proposed in self._derivations:
            for rtype, targets in (("derived_from", proposed.derived_from), ("contradicts", proposed.contradicts)):
                for target in targets:
                    resolved = self._claim_target(rtype, cid, target)
                    if resolved is not None:
                        self._link(rtype, cid, resolved, proposed.reasoning)
        self._derivations.clear()

    def _claim_target(self, rtype: str, source_id: str, target: str) -> str | None:
        try:
            resolved = self.resolve(target)
        except ValueError as err:
            self.batch.skipped.append(f"{rtype} {source_id} -> {target}: {err}")
            return None
        if self._claim_of(resolved) is None:
            self.batch.skipped.append(f"{rtype} {source_id} -> {target}: {resolved} is not a claim on record")
            return None
        return resolved

    def resolve(self, ref: str) -> str:
        if re.match(r"^(C|H|I|EXP|INT)-\d+$", ref):  # already a record ID (from the KB slice, or allocated here)
            return ref
        if ref not in self.batch.refs:
            raise ValueError(f"unknown local ref {ref}")
        return self.batch.refs[ref]

    def _claim_of(self, claim_id: str) -> Claim | None:
        """The claim an ID names, whether it was already in the KB or allocated in this batch."""
        record = self.known.get(claim_id) or next((r for r in self.batch.records if r.id == claim_id), None)
        return record if isinstance(record, Claim) else None

    def _sources_of(self, claim_id: str) -> list[str]:
        """The references a claim cites, in one form.

        A relation never writes a reference of its own: it copies what its source claim cites. So the bare
        DOIs and PMCIDs found on relations are **inherited**, not typed by the verifier, and many come from
        claims written before the declaration rule existed. The knowledge base is append-only and those claims are
        never rewritten, so canonicalising here is the only place the backlog can be put into one form, and
        it loses nothing: the agent's original spelling stays in the source claim's own record.
        """
        claim = self._claim_of(claim_id)
        return canonicalise_refs(claim.source_refs)[0] if claim is not None else []

    def relation(self, rtype: str, source: str, target: str, rationale: str | None = None) -> None:
        # The evidence of an asserted relation is what its source claim cites; the agent's reason is kept apart.
        refs = self._sources_of(source) if rtype in ("supports", "contradicts") else []
        self.batch.records.append(
            Relation(
                id=self.allocate("relation"),
                source_id=source,
                target_id=target,
                type=rtype,
                justification_method="agent_inference",
                provenance_status="unverified",
                source_refs=refs,
                rationale=rationale,
                **self._stamp(),
            )
        )

    def literature(self, output: LiteratureOutput) -> Batch:
        for proposed in output.claims:
            # A claim without a source is the agent's own knowledge, not literature evidence.
            self.claim(proposed, "literature" if proposed.source else "agent")
        self._flush_derivations()
        return self._ordered()

    def hypothesis(self, output: GenerateOutput) -> Batch:
        h = output.hypothesis
        claim_ids = [self.claim(c, "literature" if c.source else "agent") for c in h.claims]
        hid = self.hypothesis_id = self.allocate("hypothesis")
        self.batch.records.append(
            Hypothesis(
                id=hid,
                statement=h.statement,
                mechanism=h.mechanism,
                author_confidence=h.author_confidence,
                agent_id=self.agent_id,
                **self._stamp(),
            )
        )
        for cid in claim_ids:
            self.relation("part_of", cid, hid)
        self._flush_derivations()
        for link in h.links:
            self._link(link.type, link.source, link.target, link.rationale)
        for gap in h.ignorance:
            iid = self.allocate("ignorance")
            self.batch.records.append(
                Ignorance(id=iid, content=gap.content, hypothesis_id=hid, agent_id=self.agent_id, **self._stamp())
            )
            for target in gap.questions:
                self._link("questions", iid, target)
        return self._ordered()

    def review(self, output: ReviewOutput, hypothesis_id: str) -> Batch:
        self.hypothesis_id = hypothesis_id
        for proposed in output.claims:
            self.claim(proposed, "literature" if proposed.source else "agent")
        self._flush_derivations()
        for link in output.links:
            self._link(link.type, link.source, link.target, link.rationale)
        for gap in output.gaps:
            iid = self.allocate("ignorance")
            self.batch.records.append(
                Ignorance(
                    id=iid, content=gap.content, hypothesis_id=hypothesis_id, agent_id=self.agent_id, **self._stamp()
                )
            )
            for target in gap.questions:
                self._link("questions", iid, target)
        return self._ordered()

    def computed(self, content: str, claim_type: str, source_refs: list[str]) -> str:
        """A claim licensed by a verified computation (the `compute` phase; no agent is involved).

        `verification_method: code` and `provenance_status: verified` say what holds: re-running the analysis on
        the inputs named in `source_refs` reproduces this content. The references are the dataset versions and the
        artifact digest, so a changed input or a changed script yields a different claim rather than the same one.
        """
        cid = self.allocate("claim")
        self.batch.records.append(
            Claim(
                id=cid,
                canonical_hash=canonical_hash(content, None),
                type=claim_type,
                content=content,
                origin="computation",
                provenance_status="verified",
                verification_method="code",
                source_refs=list(source_refs),
                **self._stamp(),
            )
        )
        return cid

    def _link(self, rtype: str, source: str, target: str, rationale: str | None = None) -> None:
        """One proposed relation; a faulty one is skipped with its reason, the rest of the output is kept.

        A `contradicts` from a claim that is not verified cannot enter the KB: `Relation._rules` refuses it when
        the claim cites nothing, `validate._relation` when it cites something unverified, and the second message
        already prescribes the remedy ("record it as questions instead"). Rather than losing the objection, it is
        moved one step down the epistemic ladder by `_as_question`, which nothing else performed.
        """
        try:
            source_id, target_id = self.resolve(source), self.resolve(target)
            claim = self._claim_of(source_id)
            if rtype == "contradicts" and claim is not None and claim.provenance_status != "verified":
                self._as_question(source_id, target_id, rationale)
                return
            self.relation(rtype, source_id, target_id, rationale)
        except ValueError as err:
            self.batch.skipped.append(
                f"{rtype} {source} -> {target}: {err.errors()[0]['msg'] if hasattr(err, 'errors') else err}"
            )

    def _as_question(self, source_id: str, target_id: str, rationale: str | None) -> None:
        """A refused `contradicts` kept as a declared gap: an `Ignorance` carrying the author's own rationale, and
        a `questions` relation onto the same target.

        This asserts strictly less than the objection did, not more: `status.statuses` weighs only `supports` and
        `contradicts`, so neither the target's epistemic status nor the synthesis gate's verdict moves. The
        conversion is listed in `batch.converted`, so a phase report says *converted* and never silently *dropped*.
        """
        was = f"unverified objection (was: contradicts {source_id} -> {target_id})"
        iid = self.allocate("ignorance")
        self.batch.records.append(
            Ignorance(
                id=iid,
                content=f"{was}: {rationale}" if rationale else was,
                hypothesis_id=self.hypothesis_id,
                agent_id=self.agent_id,
                **self._stamp(),
            )
        )
        self.relation("questions", iid, target_id, rationale)
        self.batch.converted.append(
            f"contradicts {source_id} -> {target_id}: source claim is not verified, kept as {iid} questions {target_id}"
        )

    def confirmed(self, relation: Relation, verdict: Verdict) -> Batch:
        """A verified copy of an agent's relation, after an independent verifier confirmed it on the quote alone.
        The original stays as it was (append-only): only the new record is active."""
        if verdict.verdict != "confirmed" or not verdict.faithful:
            raise ValueError(f"{relation.id}: only a confirmed relation on a faithful claim can be verified")
        self.batch.records.append(
            Relation(
                id=self.allocate("relation"),
                source_id=relation.source_id,
                target_id=relation.target_id,
                type=relation.type,
                justification_method="quote",
                provenance_status="verified",
                source_refs=list(self._sources_of(relation.source_id)) or list(relation.source_refs),
                rationale=f"confirmed by {self.agent_id} (verifies {relation.id}): {verdict.reason}",
                **self._stamp(),
            )
        )
        return self._ordered()

    def narrowed(self, relation: Relation, verdict: Verdict) -> Batch:
        """A verdict that confirms the relation on the quote while judging the source claim overstated.

        Both halves of that verdict are information: the quote does bear on the target, and the claim as written
        says more than the quote. Requiring `faithful` before anything is written would throw both away, and with
        them most confirmed contradictions, so that no hypothesis would ever be contradicted.
        Here the verifier's restatement, narrowed to the quote, becomes a **new** claim that goes through the
        same provenance check as any other (`self.verifier`, the quote must be found in the stored paper, and
        the new claim gets its own receipt); the relation is re-asserted from it and a `derived_from` keeps the
        lineage. The overstated claim is left **exactly** as it was — in a live run that means still `verified`,
        since `_verify_candidates` only offers relations whose source claim is verified, and `verified` has only
        ever meant that its quote was found in the paper, not that the claim is faithful to it. Nothing is
        corrected in place; what the verifier found is recorded as an `Ignorance` questioning the overstated
        claim, the same demotion `_as_question` applies to an unverified objection.
        """
        if verdict.verdict != "confirmed" or verdict.faithful:
            raise ValueError(f"{relation.id}: narrowing applies to a confirmed verdict on an unfaithful claim")
        if not verdict.narrowed:
            raise ValueError(f"{relation.id}: confirmed on an overstated claim, but no restatement was offered")
        source = self._claim_of(relation.source_id)
        if source is None:
            raise ValueError(f"{relation.id}: source {relation.source_id} is not a claim")
        if source.causal_fields() is not None:
            # The overstatement of a causal claim can sit in its cause, effect or context, which a prose
            # restatement does not touch; narrowing one would silently keep the asserted edge. Reported, not done.
            raise ValueError(f"{relation.id}: a causal claim cannot be narrowed by restating its prose alone")
        # The store deduplicates claims by `canonical_hash`, so a restatement that repeats a claim already in
        # the KB is aliased to it and the relations are redirected. Where that lands decides whether the
        # narrowing means anything: onto another verified claim it is a welcome convergence (a claim an
        # earlier run had already made), but onto an endpoint of this very
        # relation it makes a self-loop, and onto an unverified claim it silently rests the "verified" relation
        # on unverified evidence. Both are refused by name.
        already = next(
            (
                r
                for r in self.known.values()
                if isinstance(r, Claim) and r.canonical_hash == canonical_hash(verdict.narrowed)
            ),
            None,
        )
        if already is not None and already.id in (relation.source_id, relation.target_id):
            raise ValueError(f"{relation.id}: the restatement repeats {already.id}, an endpoint of the relation")
        if already is not None and already.provenance_status != "verified":
            raise ValueError(f"{relation.id}: the restatement repeats {already.id}, which is not verified")
        refs = canonicalise_refs([r for r in source.source_refs if not r.startswith("receipt:")])[0]
        if not refs:
            raise ValueError(f"{relation.id}: source claim {source.id} cites no source to narrow it to")
        provenance = self.verifier(
            ProposedClaim(
                ref="narrowed", type=source.type, content=verdict.narrowed, source=refs[0], quote=source.quote
            )
        )
        if provenance.status != "verified":
            raise ValueError(f"{relation.id}: the restatement's quote could not be verified against {refs[0]}")
        cid = self.allocate("claim")
        self.batch.records.append(
            Claim(
                id=cid,
                canonical_hash=canonical_hash(verdict.narrowed),
                type=source.type,
                content=verdict.narrowed,
                origin=source.origin,
                provenance_status=provenance.status,
                verification_method=provenance.method,
                source_refs=[*refs, *provenance.refs],
                entity_refs=list(source.entity_refs),
                quote=source.quote,
                **self._stamp(),
            )
        )
        for rtype, target, rationale in (
            (
                "derived_from",
                source.id,
                f"narrowed to its quote by {self.agent_id} (verifies {relation.id}): {verdict.reason}",
            ),
            (
                relation.type,
                relation.target_id,
                f"confirmed by {self.agent_id} (verifies {relation.id}) on a restatement narrowed to the "
                f"quote: {verdict.reason}",
            ),
        ):
            self.batch.records.append(
                Relation(
                    id=self.allocate("relation"),
                    source_id=cid,
                    target_id=target,
                    type=rtype,
                    justification_method="quote",
                    provenance_status="verified",
                    source_refs=[*refs, *provenance.refs],
                    rationale=rationale,
                    **self._stamp(),
                )
            )
        iid = self.allocate("ignorance")
        self.batch.records.append(
            Ignorance(
                id=iid,
                content=f"claim {source.id} says more than its quote (narrowed to {cid} by "
                f"{self.agent_id}): {verdict.reason}",
                agent_id=self.agent_id,
                **self._stamp(),
            )
        )
        self.batch.records.append(
            Relation(
                id=self.allocate("relation"),
                source_id=iid,
                target_id=source.id,
                type="questions",
                justification_method="agent_inference",
                provenance_status="unverified",
                rationale=f"overstates its quote (verifies {relation.id})",
                **self._stamp(),
            )
        )
        self.batch.converted.append(
            f"{relation.type} {relation.source_id} -> {relation.target_id}: confirmed on the quote but the claim "
            f"overstated it, kept as {cid} narrowed to the quote, with {iid} questioning {source.id}"
        )
        return self._ordered()

    @staticmethod
    def _dataset_id(ref: str) -> str:
        """`dataset:michaelis-table1-epitopes@89af13c3630a` and the bare id both name the same source."""
        return ref.removeprefix("dataset:").split("@", 1)[0].strip()

    def experiments(self, output: DesignOutput, hypothesis_id: str, datasets: set[str] | None = None) -> Batch:
        """One Experiment per proposal plus a `tests` relation to each claim it tests.

        `datasets` are the source ids this run's manifest holds. An experiment calling itself local while naming a
        dataset the run does not have is skipped with the reason: it would claim a provenance nobody can check.
        """
        for proposed in output.experiments:
            eid = self.allocate("experiment")
            if proposed.dataset_location == "local" and datasets is not None:
                unknown = [r for r in proposed.dataset_refs if self._dataset_id(r) not in datasets]
                if unknown:
                    self.batch.skipped.append(
                        f"experiment {proposed.title!r}: local dataset(s) not in this run's manifest: {unknown}"
                    )
                    continue
            fields = proposed.model_dump() | self._normalised(eid, proposed, datasets)
            try:
                self.batch.records.append(
                    Experiment(id=eid, hypothesis_id=hypothesis_id, agent_id=self.agent_id, **fields, **self._stamp())
                )
            except ValueError as err:
                self.batch.skipped.append(
                    f"experiment {proposed.title!r}: {err.errors()[0]['msg'] if hasattr(err, 'errors') else err}"
                )
                continue
            for cid in dict.fromkeys(fields["tested_claims"]):
                self._link("tests", eid, cid)
        return self._ordered()

    def _normalised(self, eid: str, proposed: ProposedExperiment, datasets: set[str] | None) -> dict[str, Any]:
        """Two fields a designer gets wrong mechanically, derived instead of refused.

        Refusing a proposal for naming datasets without saying whether they are local or remote, or for an
        expected outcome about a claim absent from `tested_claims`, would drop most experiments for reasons that
        are not about the science. The principle is to reject an item for a real reason, not for a formatting
        one. So:

        - `dataset_location` is a **fact about the run**, not an opinion of the agent: a dataset the run's
          manifest holds is local, anything else would have to be acquired. Derived only when the agent left the
          field empty, so an explicit `local` on a dataset the run does not have is still refused above.
        - an outcome about a claim the experiment does not list is read as what it plainly says — the experiment
          bears on that claim — so the claim joins `tested_claims` and gets its `tests` relation.

        Every derivation is reported in `converted`, never silently.
        """
        out: dict[str, Any] = {}
        if proposed.dataset_refs and proposed.dataset_location is None and datasets is not None:
            local = all(self._dataset_id(r) in datasets for r in proposed.dataset_refs)
            out["dataset_location"] = "local" if local else "remote"
            self.batch.converted.append(
                f"experiment {eid}: dataset_location was not given, derived {out['dataset_location']} from this "
                f"run's manifest"
            )
        outside = [
            c for c in dict.fromkeys(o.claim_id for o in proposed.expected_outcomes) if c not in proposed.tested_claims
        ]
        if outside:
            out["tested_claims"] = [*proposed.tested_claims, *outside]
            self.batch.converted.append(
                f"experiment {eid}: outcomes named {outside} without listing them as tested, added to " f"tested_claims"
            )
        return out

    def interventions(self, output: InterveneOutput, hypothesis_id: str) -> Batch:
        """One Intervention per candidate plus a `depends_on` relation to each claim it rests on."""
        for proposed in output.candidates:
            iid = self.allocate("intervention")
            try:
                self.batch.records.append(
                    Intervention(
                        id=iid,
                        hypothesis_id=hypothesis_id,
                        agent_id=self.agent_id,
                        **proposed.model_dump(),
                        **self._stamp(),
                    )
                )
            except ValueError as err:
                self.batch.skipped.append(
                    f"intervention {proposed.name!r}: {err.errors()[0]['msg'] if hasattr(err, 'errors') else err}"
                )
                continue
            for cid in dict.fromkeys(proposed.acts_on_claims):
                self._link("depends_on", iid, cid)
        return self._ordered()

    def decision(self, output: ArbitrateOutput, hypothesis_id: str) -> Batch:
        self.batch.records.append(
            Decision(
                id=self.allocate("decision"),
                hypothesis_id=hypothesis_id,
                agent_id=self.agent_id,
                **output.model_dump(),
                **self._stamp(),
            )
        )
        return self._ordered()

    def _ordered(self) -> Batch:
        rank = {
            Entity: 0,
            Claim: 1,
            Hypothesis: 2,
            Ignorance: 3,
            Experiment: 4,
            Intervention: 5,
            Relation: 6,
            Decision: 7,
        }
        self.batch.records.sort(key=lambda r: rank[type(r)])  # stable: keeps the order within each type
        return self.batch
