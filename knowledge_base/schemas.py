"""Record types of the knowledge base and the rules a single record can check on its own.

Records are stored, append-only, one JSONL file per type: `Claim` (C-*), `Relation` (L-*), `Entity` (N-*),
`Hypothesis` (H-*), `Ignorance` (I-*), `Event` (V-*). Rules that need more than one record (which endpoint types a
relation admits, whether the source of a `contradicts` is verified, duplicates across runs) are not here: they
belong to the validator and the store.

Two statuses are kept apart on purpose:

- `provenance_status` (stored) says whether a record really comes from its source;
- the epistemic status of a claim (supported, contradicted, ...) is never stored: it is computed from the
  verified relations pointing at the claim, so no record type has a field for it.

All models are frozen and reject unknown fields, so an agent cannot smuggle a status in.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime
from typing import Annotated, Final, Literal, NamedTuple

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

PREFIXES: Final = {
    "claim": "C",
    "relation": "L",
    "entity": "N",
    "hypothesis": "H",
    "ignorance": "I",
    "event": "V",
    "experiment": "EXP",
    "decision": "D",
    "intervention": "INT",
}
# Records a relation may connect: C-, H-, I-; EXP- as the source of `tests`; INT- as the source of `depends_on`.
ENDPOINT_PREFIXES: Final = ("C", "H", "I", "EXP", "INT")

NonEmpty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
ClaimId = Annotated[str, StringConstraints(pattern=r"^C-\d+$")]
RelationId = Annotated[str, StringConstraints(pattern=r"^L-\d+$")]
EntityId = Annotated[str, StringConstraints(pattern=r"^N-\d+$")]
HypothesisId = Annotated[str, StringConstraints(pattern=r"^H-\d+$")]
IgnoranceId = Annotated[str, StringConstraints(pattern=r"^I-\d+$")]
EventId = Annotated[str, StringConstraints(pattern=r"^V-\d+$")]
ExperimentId = Annotated[str, StringConstraints(pattern=r"^EXP-\d+$")]
DecisionId = Annotated[str, StringConstraints(pattern=r"^D-\d+$")]
InterventionId = Annotated[str, StringConstraints(pattern=r"^INT-\d+$")]
EndpointId = Annotated[str, StringConstraints(pattern=r"^(C|H|I|EXP|INT)-\d+$")]
RecordId = Annotated[str, StringConstraints(pattern=r"^(C|L|N|H|I|V|EXP|D|INT)-\d+$")]

# What an agent may assign to a claim. `causal` is **not** here on purpose: whether a claim asserts a cause is
# decided by the presence of the causal block, not by a label. The old enum put the shape of the assertion
# (`causal`) and the kind of statement (`observational`, `quantitative`) on one mutually exclusive field, and
# agents resolved the clash by typing the evidence and filling the block, which got the claims refused.
StatementType = Literal["observational", "quantitative", "methodological", "assumption"]
# What the store accepts. `causal` is kept as a legacy value: claims written under the old enum carry it, and
# the knowledge base must stay readable exactly as it was written.
ClaimType = Literal["causal", "observational", "quantitative", "methodological", "assumption"]
Origin = Literal["literature", "agent", "computation", "experiment"]
ProvenanceStatus = Literal["verified", "unverified"]
VerificationMethod = Literal["quote", "code", "symbolic", "experiment", "none"]
JustificationMethod = Literal["quote", "code", "symbolic", "experiment", "agent_inference"]
RelationType = Literal[
    "supports",
    "contradicts",
    "questions",
    "tests",
    "part_of",
    "derived_from",
    "depends_on",
    "supersedes",
]
# Legacy (ADR 0032): read on records written before the sign left the causal block, never written now.
Sign = Literal["positive", "negative"]
Modality = Literal[
    "small_molecule",
    "biologic",
    "peptide",
    "antigen_specific",
    "cell_therapy",
    "gene_therapy",
    "repurposed_drug",
    "other",
]
EventType = Literal["written", "rejected", "seen_again"]
Level = Literal["low", "medium", "high"]
# What it would take to run an experiment. `in_silico` is the one kind this system can execute itself, on a dataset
# the run declares; `wet_lab` (trial, cohort, in vivo) is a proposal for someone else to fund. The distinction is orthogonal to whether the dataset is already local.
ExperimentKind = Literal["wet_lab", "in_silico"]
DatasetLocation = Literal["local", "remote"]
DecisionType = Literal["buy", "rejected", "stop"]

CAUSAL_FIELDS: Final = ("cause_ref", "effect_ref", "context", "mechanism")
# The order the identity hash has always used. `sign` stays in it so that a record written before ADR 0032, which
# carries one, keeps the hash it was written with; a record without it hashes without it.
HASH_ORDER: Final = ("cause_ref", "effect_ref", "sign", "context", "mechanism")


def normalize(text: str) -> str:
    """Canonical form used for duplicate detection: NFKC, case-folded, whitespace collapsed, outer
    punctuation stripped. Deliberately shallow: it merges copies of one sentence, not paraphrases."""
    folded = unicodedata.normalize("NFKC", text).casefold()
    return re.sub(r"\s+", " ", folded).strip(" .;:,!?\"'")


class EntityRule(NamedTuple):
    """One named rewrite applied when matching entity names. Named so a merge can be explained."""

    name: str
    pattern: str
    replacement: str


# Applied in order, after `normalize`, to decide whether two entity names are the same thing. Each is a
# shape an agent varies freely and that no reader would call a different entity. Deliberately conservative:
# nothing here merges two words, so `IL-10` never meets `IL-4`, and the `+` of `CD4+` is preserved because
# it is part of the name. The list is explicit rather than clever, so a surprising merge can be traced to
# the rule that caused it.
ENTITY_RULES: Final[tuple[EntityRule, ...]] = (
    EntityRule("leading_article", r"^(?:the|a|an)\s+", ""),
    EntityRule("parenthetical", r"\s*\([^)]*\)", ""),  # "C9orf72 (protein)" -> "c9orf72"
    EntityRule("possessive", r"'s\b", ""),
    EntityRule("separator", r"[\u2010-\u2015_/\\]+", "-"),  # en/em dashes, underscore, slash
    EntityRule("spaced_hyphen", r"\s*-\s*", "-"),  # "IL - 10" -> "il-10"
    EntityRule("gene_number", r"\b([a-z]{2,6})-(\d+)\b", r"\1\2"),  # "il-10" -> "il10", "cd-4" -> "cd4"
    EntityRule("word_hyphen", r"\b([a-z]+)-([a-z]{2,})\b", r"\1 \2"),  # "T-cell" -> "t cell"; after gene_number
    EntityRule("plural", r"\b([a-z]{4,}?)s\b", r"\1"),  # "cells" -> "cell", "responses" -> "response"
    EntityRule("whitespace", r"\s+", " "),
)


def entity_key(name: str) -> str:
    """The form two entity names must share to be the same entity.

    Stronger than `normalize`, and kept separate from it on purpose: `normalize` feeds `canonical_hash`,
    so changing it would change the identity of every claim already stored. This one is used only when
    the host decides whether an entity already exists.

    It is still a string rule. It merges spellings, never meanings: `IL-10-mediated T cell responses` and
    `IL-10 responses` stay two entities, which is the larger half of the problem and needs a decision this
    function cannot make.
    """
    key = normalize(name)
    for rule in ENTITY_RULES:
        key = re.sub(rule.pattern, rule.replacement, key)
    return key.strip(" -")


def entity_rule_trace(name: str) -> list[tuple[str, str]]:
    """Each rule that changed the name, with the result, so a merge can be explained rather than trusted."""
    trace, key = [], normalize(name)
    for rule in ENTITY_RULES:
        after = re.sub(rule.pattern, rule.replacement, key)
        if after != key:
            trace.append((rule.name, after))
        key = after
    return trace


def canonical_hash(content: str, causal: dict[str, str] | None = None) -> str:
    """sha256 of the normalized content; for a causal claim the normalized causal fields are included,
    in the fixed order of HASH_ORDER, so two causal claims with the same prose but a different cause, effect
    or context are different claims. A legacy `sign` is hashed only when the mapping carries one."""
    parts = [normalize(content)]
    if causal is not None:
        parts += [f"{name}={normalize(causal[name])}" for name in HASH_ORDER if name in causal]
    return "sha256:" + hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


class _Record(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    run_id: NonEmpty
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("created_at must carry a timezone")
        return value


class Claim(_Record):
    id: ClaimId
    canonical_hash: NonEmpty
    type: ClaimType
    content: NonEmpty
    origin: Origin
    provenance_status: ProvenanceStatus
    verification_method: VerificationMethod
    source_refs: list[NonEmpty] = Field(default_factory=list)
    # What the agent actually wrote, before the host put the references into one form. Kept because the
    # canonical form is a normalisation and provenance is about what was said, not about what the host made
    # of it. Empty on a record written before this field existed and on one whose references needed no change.
    source_refs_raw: list[NonEmpty] = Field(default_factory=list)
    entity_refs: list[EntityId] = Field(default_factory=list)
    quote: NonEmpty | None = None  # verbatim passage of the source; not part of the claim's identity
    # The causal block: all four together or none of them. Carrying it is what makes a claim causal, whatever
    # its `type` says about the kind of statement it is. The direction of the effect is in the entity names.
    cause_ref: EntityId | None = None
    effect_ref: EntityId | None = None
    context: NonEmpty | None = None
    mechanism: NonEmpty | None = None
    # Legacy (ADR 0032): the sign was never defined and agents used it inconsistently with the names. Kept only
    # so that a record written with it still reads and keeps its hash; nothing writes or reads it any more.
    sign: Sign | None = None

    def causal_fields(self) -> dict[str, str] | None:
        """The causal block, or `None` when the claim carries none. Its presence is the definition of a causal
        claim here — the `type` field says what kind of statement it is, not whether it asserts a cause."""
        if any(getattr(self, name) is None for name in CAUSAL_FIELDS):
            return None
        return {name: str(getattr(self, name)) for name in CAUSAL_FIELDS}

    def identity_fields(self) -> dict[str, str] | None:
        """What the identity hash covers: the causal block, plus the legacy sign of a record that has one."""
        causal = self.causal_fields()
        if causal is not None and self.sign is not None:
            causal["sign"] = self.sign
        return causal

    @model_validator(mode="after")
    def _rules(self) -> Claim:
        present = [name for name in CAUSAL_FIELDS if getattr(self, name) is not None]
        if present and len(present) != len(CAUSAL_FIELDS):
            missing = sorted(set(CAUSAL_FIELDS) - set(present))
            raise ValueError(f"causal claim {self.id} lacks {missing}")
        if self.type == "causal" and not present:
            # Only reachable through the legacy type value: a claim labelled causal that names no cause.
            raise ValueError(f"claim {self.id} is typed causal but carries no causal block")
        if self.sign is not None and not present:
            raise ValueError(f"claim {self.id} carries a sign but no causal block")
        if present and self.cause_ref == self.effect_ref:
            raise ValueError(f"causal claim {self.id} has the same entity as cause and effect")
        if self.provenance_status == "verified":
            if self.verification_method == "none":
                raise ValueError(f"claim {self.id} is verified with verification_method 'none'")
            if not self.source_refs:
                raise ValueError(f"claim {self.id} is verified without source_refs")
        if self.origin == "literature" and not self.source_refs:
            raise ValueError(f"literature claim {self.id} has no source_refs")
        expected = canonical_hash(self.content, self.identity_fields())
        if self.canonical_hash != expected:
            raise ValueError(f"claim {self.id}: canonical_hash does not match its content")
        return self


class Relation(_Record):
    id: RelationId
    source_id: EndpointId
    target_id: EndpointId
    type: RelationType
    justification_method: JustificationMethod
    provenance_status: ProvenanceStatus
    source_refs: list[NonEmpty] = Field(default_factory=list)
    rationale: NonEmpty | None = None  # why the proposer asserts the relation; not evidence

    @model_validator(mode="after")
    def _rules(self) -> Relation:
        if self.source_id == self.target_id:
            raise ValueError(f"relation {self.id} connects {self.source_id} to itself")
        if self.provenance_status == "verified":
            if self.justification_method == "agent_inference":
                raise ValueError(f"relation {self.id}: an agent_inference cannot be verified")
            if not self.source_refs:
                raise ValueError(f"relation {self.id} is verified without source_refs")
        if self.type == "contradicts" and not self.source_refs:
            raise ValueError(f"contradicts relation {self.id} has no explicit evidence reference")
        return self


class Entity(_Record):
    id: EntityId
    canonical_name: NonEmpty
    aliases: list[NonEmpty] = Field(default_factory=list)

    @model_validator(mode="after")
    def _rules(self) -> Entity:
        names = [normalize(name) for name in (self.canonical_name, *self.aliases)]
        if len(set(names)) != len(names):
            raise ValueError(f"entity {self.id} repeats a name among canonical_name and aliases")
        return self


class Hypothesis(_Record):
    """Proposed by an agent. Its claims are linked to it by `part_of`, its open issues by `questions`."""

    id: HypothesisId
    statement: NonEmpty
    mechanism: NonEmpty
    author_confidence: Annotated[int, Field(ge=0, le=5)]
    agent_id: NonEmpty


class Ignorance(_Record):
    """An issue an agent declares it cannot resolve. It is never negative evidence."""

    id: IgnoranceId
    content: NonEmpty
    hypothesis_id: HypothesisId | None = None
    agent_id: NonEmpty


class Event(_Record):
    """One line per write, rejection or re-encounter of a record. Kept minimal on purpose."""

    id: EventId
    event: EventType
    record_id: RecordId | None = None
    agent_id: NonEmpty | None = None
    detail: NonEmpty | None = None

    @model_validator(mode="after")
    def _rules(self) -> Event:
        if self.event in ("written", "seen_again") and self.record_id is None:
            raise ValueError(f"event {self.id} ({self.event}) needs a record_id")
        if self.event == "seen_again" and not self.record_id.startswith("C-"):
            raise ValueError(f"event {self.id}: seen_again applies to claims only")
        if self.event == "rejected" and self.detail is None:
            raise ValueError(f"event {self.id}: a rejection must say why (detail)")
        return self


class ExpectedOutcome(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    outcome: NonEmpty  # what would be observed
    claim_id: ClaimId  # the tested claim it bears on
    effect: Literal["supports", "contradicts"]


class Experiment(_Record):
    """A proposed experiment (never a performed one). Linked to the claims it tests by `tests` relations."""

    id: ExperimentId
    hypothesis_id: HypothesisId
    title: NonEmpty
    protocol: NonEmpty
    tested_claims: list[ClaimId] = Field(min_length=1)
    expected_outcomes: list[ExpectedOutcome] = Field(min_length=2)
    cost: Level  # qualitative on purpose: an LLM's figure in USD is not checkable
    cost_rationale: NonEmpty
    feasibility: Level
    kind: ExperimentKind = "wet_lab"  # default keeps every earlier record readable
    # The datasets an `in_silico` experiment would run on, by the id the run's data manifest gives them
    # (`dataset:<id>@<hash>` or the bare id), or an accession for one not yet acquired.
    dataset_refs: list[NonEmpty] = Field(default_factory=list)
    dataset_location: DatasetLocation | None = None
    required_markers: list[NonEmpty] = Field(default_factory=list)
    required_metadata: list[NonEmpty] = Field(default_factory=list)
    analysis_id: Literal["hla_panel_compact_v1", "ics_cytokine_skew_v1"] | None = None
    agent_id: NonEmpty

    @model_validator(mode="after")
    def _rules(self) -> Experiment:
        if self.kind == "in_silico" and not self.dataset_refs:
            raise ValueError(f"experiment {self.id} is in_silico but names no dataset: there is nothing to run it on")
        if self.dataset_refs and self.dataset_location is None:
            raise ValueError(f"experiment {self.id} names datasets without saying whether they are local or remote")
        if self.kind == "wet_lab" and self.dataset_location == "remote" and not self.dataset_refs:
            raise ValueError(f"experiment {self.id}: a remote location without a dataset means nothing")
        if self.analysis_id and (self.kind != "in_silico" or self.dataset_location != "local"):
            raise ValueError(f"experiment {self.id}: registered analysis requires local in_silico inputs")
        outside = {o.claim_id for o in self.expected_outcomes} - set(self.tested_claims)
        if outside:
            raise ValueError(f"experiment {self.id}: outcomes about claims it does not test: {sorted(outside)}")
        if not any(o.effect == "contradicts" for o in self.expected_outcomes):
            raise ValueError(
                f"experiment {self.id} has no outcome that would contradict a tested claim: not falsifying"
            )
        return self


class Decision(_Record):
    """The arbiter's decision on a hypothesis: buy the information (run these experiments), reject it, or stop."""

    id: DecisionId
    hypothesis_id: HypothesisId
    decision: DecisionType
    experiment_ids: list[ExperimentId] = Field(default_factory=list)
    value_of_information: Level
    rationale: NonEmpty
    agent_id: NonEmpty

    @model_validator(mode="after")
    def _rules(self) -> Decision:
        if self.decision == "buy" and not self.experiment_ids:
            raise ValueError(f"decision {self.id}: buy needs at least one experiment")
        if self.decision != "buy" and self.experiment_ids:
            raise ValueError(f"decision {self.id}: only a buy decision names experiments")
        return self


class Intervention(_Record):
    """A proposed therapeutic candidate (never an administered one), tied to the mechanistic claims it acts on.

    Outputs C, D and E of a final proposal live here: the candidate itself, who it would be given to, and
    what would be measured. A candidate must state at least one off-target risk and one safety concern: the
    absence of a stated risk is not evidence of safety, so an empty list would read as a claim the agent cannot
    make. `acts_on_claims` are the mechanistic steps the candidate assumes, linked by `depends_on` relations, so
    a candidate falls with the claims under it.
    """

    id: InterventionId
    hypothesis_id: HypothesisId
    name: NonEmpty
    modality: Modality
    target: NonEmpty  # the molecule, cell population or process acted upon
    acts_on_claims: list[ClaimId] = Field(min_length=1)
    rationale: NonEmpty
    predicted_effect: NonEmpty  # what should change if the mechanism holds, stated so it can fail
    off_target_risks: list[NonEmpty] = Field(min_length=1)
    safety_concerns: list[NonEmpty] = Field(min_length=1)
    stratification: NonEmpty  # output D: which patients, and on what measurable criterion
    biomarker: NonEmpty  # output E: what to measure to tell whether it works
    maturity: Level  # how far the modality itself is from the clinic, qualitatively
    drug_name: NonEmpty | None = None  # a real compound's name, only if the agent is sure; free text
    agent_id: NonEmpty
