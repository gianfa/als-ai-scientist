"""Per-record rules of the knowledge base."""

from __future__ import annotations

from datetime import datetime

import pytest
from kb_factories import CAUSAL, NOW, claim, decision, experiment, hypothesis, ignorance, intervention, relation
from pydantic import ValidationError

from knowledge_base.schemas import Entity, Event, canonical_hash, normalize

# --- canonical hash ---------------------------------------------------------------------------------------


def test_normalize_merges_copies_of_one_sentence_not_paraphrases():
    assert normalize("  DPRs  are\nDetected. ") == normalize("dprs are detected")
    assert normalize("DPRs are detected") != normalize("DPRs were detected")


def test_causal_fields_enter_the_hash():
    plain = canonical_hash("C9orf72 expansion drives DPR production")
    causal = canonical_hash("C9orf72 expansion drives DPR production", {k: str(v) for k, v in CAUSAL.items()})
    elsewhere = canonical_hash(
        "C9orf72 expansion drives DPR production", {**{k: str(v) for k, v in CAUSAL.items()}, "context": "SMA"}
    )
    assert len({plain, causal, elsewhere}) == 3


# --- ADR 0032: the legacy sign ---------------------------------------------------------------------------

LEGACY = {**CAUSAL, "sign": "positive"}


def _hash_as_written_before_adr_0032(content: str) -> str:
    """The hash a record carrying a sign was written with: five fields in their original order."""
    import hashlib

    order = ("cause_ref", "effect_ref", "sign", "context", "mechanism")
    parts = [normalize(content)] + [f"{k}={normalize(str(LEGACY[k]))}" for k in order]
    return "sha256:" + hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def test_a_record_written_with_a_sign_still_reads_and_keeps_its_hash():
    content = "C9orf72 expansion drives DPR production"
    old = claim(id="C-7", content=content, canonical_hash=_hash_as_written_before_adr_0032(content), **LEGACY)
    assert old.sign == "positive"
    assert "sign" not in old.causal_fields()  # nothing downstream sees it
    assert old.identity_fields()["sign"] == "positive"


def test_a_new_causal_claim_hashes_without_a_sign():
    content = "C9orf72 expansion drives DPR production"
    new = claim(id="C-8", content=content, **CAUSAL)
    assert new.sign is None and new.canonical_hash != _hash_as_written_before_adr_0032(content)


def test_a_sign_without_a_causal_block_is_refused():
    with pytest.raises(ValidationError, match="sign but no causal block"):
        claim(sign="positive")


# --- Claim ------------------------------------------------------------------------------------------------


def test_valid_claims_of_each_kind():
    assert claim().provenance_status == "verified"
    assert claim(id="C-2", type="causal", content="C9orf72 expansion drives DPR production", **CAUSAL).causal_fields()
    assert (
        claim(origin="agent", provenance_status="unverified", verification_method="none", source_refs=[]).origin
        == "agent"
    )


def test_claim_has_no_epistemic_status_field():
    with pytest.raises(ValidationError, match="epistemic_status"):
        claim(epistemic_status="supported")


@pytest.mark.parametrize("missing", list(CAUSAL))
def test_causal_claim_requires_every_causal_field(missing):
    fields = {k: v for k, v in CAUSAL.items() if k != missing}
    with pytest.raises(ValidationError, match="lacks"):
        claim(type="causal", content="x causes y", canonical_hash="sha256:any", **fields)


def test_a_partial_causal_block_is_refused():
    """All four fields or none: half an edge names no cause-effect relation."""
    with pytest.raises(ValidationError, match="lacks"):
        claim(context="ALS/FTD")


def test_the_causal_block_is_what_makes_a_claim_causal_whatever_its_type():
    """`causal` is not a type exclusive of `observational`: an observational claim that fills the causal
    block is causal. The type and the block are independent."""
    observed = claim(type="observational", content="C9orf72 loss of function impairs autophagy.", **CAUSAL)

    assert observed.causal_fields() == {k: str(v) for k, v in CAUSAL.items()}
    assert claim(type="observational", content="A plain observation.").causal_fields() is None


def test_a_claim_typed_causal_without_a_block_is_refused():
    """`causal` survives only as a legacy type, and records of that type all carry a causal block."""
    with pytest.raises(ValidationError, match="typed causal but carries no causal block"):
        claim(type="causal", content="x drives y")


def test_a_causal_claim_needs_two_distinct_entities():
    with pytest.raises(ValidationError, match="same entity"):
        claim(type="observational", content="x", **{**CAUSAL, "effect_ref": "N-1"})


def test_verified_claim_needs_a_method_and_a_source():
    with pytest.raises(ValidationError, match="verification_method 'none'"):
        claim(verification_method="none")
    with pytest.raises(ValidationError, match="without source_refs"):
        claim(origin="agent", source_refs=[])


def test_literature_claim_needs_a_source_even_when_unverified():
    with pytest.raises(ValidationError, match="literature claim"):
        claim(provenance_status="unverified", source_refs=[])


def test_hash_must_match_content():
    with pytest.raises(ValidationError, match="canonical_hash"):
        claim(canonical_hash=canonical_hash("something else"))


def test_ids_have_the_prefix_of_their_type():
    with pytest.raises(ValidationError):
        claim(id="E-1")
    with pytest.raises(ValidationError):
        relation(id="C-9")


def test_records_are_frozen_and_need_an_aware_timestamp():
    c = claim()
    with pytest.raises(ValidationError):
        c.content = "changed"  # type: ignore[misc]
    with pytest.raises(ValidationError, match="timezone"):
        claim(created_at=datetime(2026, 10, 2, 12, 0))


# --- Relation ---------------------------------------------------------------------------------------------


def test_relation_endpoints_are_claims_hypotheses_or_ignorance():
    assert (
        relation(
            source_id="I-1",
            target_id="C-1",
            type="questions",
            justification_method="agent_inference",
            provenance_status="unverified",
            source_refs=[],
        ).type
        == "questions"
    )
    assert relation(target_id="H-3", type="part_of").target_id == "H-3"
    with pytest.raises(ValidationError):
        relation(source_id="N-1")


def test_relation_cannot_loop_on_itself():
    with pytest.raises(ValidationError, match="to itself"):
        relation(target_id="C-1")


def test_agent_inference_is_never_verified():
    with pytest.raises(ValidationError, match="agent_inference"):
        relation(justification_method="agent_inference")
    assert relation(justification_method="agent_inference", provenance_status="unverified").type == "supports"


def test_contradicts_needs_an_explicit_evidence_reference():
    with pytest.raises(ValidationError, match="evidence reference"):
        relation(type="contradicts", provenance_status="unverified", source_refs=[])


# --- Entity, Hypothesis, Ignorance, Event -----------------------------------------------------------------


def test_entity_names_are_unique_after_normalization():
    assert Entity(
        id="N-1", canonical_name="C9orf72 expansion", aliases=["C9orf72 HRE"], run_id="RUN-1", created_at=NOW
    ).aliases == ["C9orf72 HRE"]
    with pytest.raises(ValidationError, match="repeats a name"):
        Entity(id="N-1", canonical_name="C9orf72 HRE", aliases=["c9orf72  hre"], run_id="RUN-1", created_at=NOW)


def test_hypothesis_confidence_is_bounded():
    assert hypothesis(author_confidence=5).author_confidence == 5
    with pytest.raises(ValidationError):
        hypothesis(author_confidence=6)


def test_ignorance_may_name_its_hypothesis():
    assert ignorance(hypothesis_id="H-1").hypothesis_id == "H-1"


def test_event_rules():
    assert Event(id="V-1", event="seen_again", record_id="C-1", run_id="RUN-2", created_at=NOW).record_id == "C-1"
    with pytest.raises(ValidationError, match="needs a record_id"):
        Event(id="V-2", event="written", run_id="RUN-2", created_at=NOW)
    with pytest.raises(ValidationError, match="claims only"):
        Event(id="V-3", event="seen_again", record_id="H-1", run_id="RUN-2", created_at=NOW)
    with pytest.raises(ValidationError, match="must say why"):
        Event(id="V-4", event="rejected", run_id="RUN-2", created_at=NOW)


def test_an_experiment_must_be_able_to_falsify():
    assert experiment().cost == "medium"
    with pytest.raises(ValidationError, match="not falsifying"):
        experiment(
            expected_outcomes=[
                {"outcome": "a", "claim_id": "C-1", "effect": "supports"},
                {"outcome": "b", "claim_id": "C-1", "effect": "supports"},
            ]
        )
    with pytest.raises(ValidationError, match="does not test"):
        experiment(
            expected_outcomes=[
                {"outcome": "a", "claim_id": "C-1", "effect": "supports"},
                {"outcome": "b", "claim_id": "C-2", "effect": "contradicts"},
            ]
        )


def test_only_a_buy_decision_names_experiments():
    assert decision().decision == "buy"
    with pytest.raises(ValidationError, match="at least one experiment"):
        decision(experiment_ids=[])
    with pytest.raises(ValidationError, match="only a buy decision"):
        decision(decision="rejected")
    assert decision(decision="rejected", experiment_ids=[]).decision == "rejected"


def test_an_intervention_states_its_risks_and_the_claims_it_rests_on():
    assert intervention().modality == "biologic"
    for field in ("off_target_risks", "safety_concerns", "acts_on_claims"):
        with pytest.raises(ValidationError, match="at least 1 item"):
            intervention(**{field: []})
    with pytest.raises(ValidationError):
        intervention(acts_on_claims=["H-1"])


def test_an_in_silico_experiment_must_name_the_data_it_would_run_on():
    """A dataset is the instrument of a proposed experiment, so `in_silico` without a dataset is an
    experiment with nothing to run on."""
    assert experiment().kind == "wet_lab" and experiment().dataset_refs == []
    with pytest.raises(ValidationError, match="names no dataset"):
        experiment(kind="in_silico")
    runnable = experiment(kind="in_silico", dataset_refs=["michaelis-table1-epitopes"], dataset_location="local")
    assert runnable.dataset_location == "local"
    with pytest.raises(ValidationError, match="whether they are local or remote"):
        experiment(kind="in_silico", dataset_refs=["GCST90027164"])


# --- entity_key: the spellings an agent varies, and the meanings it does not --------------------
# Kept separate from `normalize` on purpose: that one feeds `canonical_hash`, so changing it would change
# the identity of every claim already stored.


@pytest.mark.parametrize(
    "a,b",
    [
        ("IL-10", "IL10"),
        ("motor neurons", "Motor Neuron"),
        ("motor_neurons", "motor neurons"),
        ("T-cell response", "T cell responses"),
        ("regulatory T cells", "regulatory T cells (Treg)"),
        ("the C9orf72 protein", "C9orf72 protein"),
        ("ALS patients", "ALS_patients"),
    ],
)
def test_entity_key_merges_spellings_of_one_entity(a: str, b: str) -> None:
    from knowledge_base.schemas import entity_key

    assert entity_key(a) == entity_key(b)


@pytest.mark.parametrize(
    "a,b",
    [
        ("IL-10", "IL-4"),  # no rule merges two words
        ("CD4+ T cells", "CD8+ T cells"),
        ("IL-10 responses", "IL-10-mediated T cell responses"),  # a paraphrase, not a spelling
        ("motor neuron", "motor neuron injury"),
    ],
)
def test_entity_key_merges_no_meanings(a: str, b: str) -> None:
    """The larger half of the fragmentation is semantic and is deliberately out of reach of a string rule."""
    from knowledge_base.schemas import entity_key

    assert entity_key(a) != entity_key(b)


def test_a_space_before_a_number_is_a_known_unmerged_spelling() -> None:
    """`IL 10` does not meet `IL-10`. Joining a word to a following number would merge things that are not
    one entity, so the rule set stays conservative and the gap is asserted rather than left to surprise."""
    from knowledge_base.schemas import entity_key

    assert entity_key("IL 10") != entity_key("IL-10")


def test_entity_key_preserves_the_plus_of_a_surface_marker() -> None:
    from knowledge_base.schemas import entity_key

    assert "+" in entity_key("CD4+ T cells")


def test_a_merge_can_be_explained_by_the_rule_that_caused_it() -> None:
    """The rules are an explicit list rather than one clever regex so a surprising merge is traceable."""
    from knowledge_base.schemas import entity_rule_trace

    assert [name for name, _ in entity_rule_trace("the IL - 10 responses")] == [
        "leading_article",
        "spaced_hyphen",
        "gene_number",
        "plural",
    ]


def test_entity_key_does_not_change_claim_identity() -> None:
    """`canonical_hash` must keep using `normalize`: the store holds hashes computed with it."""
    from knowledge_base.schemas import canonical_hash, normalize

    text = "IL-10 responses are associated with longer survival"
    assert canonical_hash(text) == canonical_hash(normalize(text))
