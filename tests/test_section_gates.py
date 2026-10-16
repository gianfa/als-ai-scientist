"""Each section check is tested alone, against records built for it and nothing else.

The checks are independent by construction, so the tests are too: no test here builds a whole proposal, and a
check that is dropped from `CHECKS` takes exactly its own tests with it.
"""

from __future__ import annotations

import pytest

from pipeline.proposals import Section
from pipeline.section_gates import (
    CHECKS,
    check_biomarker,
    check_stratification,
    check_therapeutic_hypothesis,
    check_translation,
    cited_ids,
)


def section(text: str = "", cites: list[str] | None = None) -> Section:
    return Section(text=text or "prose", cites=cites or [])


class FakeHypothesis:
    pass


class FakeOutcome:
    def __init__(self, effect: str) -> None:
        self.effect = effect


class FakeExperiment:
    def __init__(self, kind: str = "wet_lab", effects: tuple[str, ...] = ("supports", "contradicts")) -> None:
        self.kind = kind
        self.expected_outcomes = [FakeOutcome(e) for e in effects]


class FakeIntervention:
    def __init__(self, stratification: str = "C9orf72 carriers", biomarker: str = "serum IL-10") -> None:
        self.stratification = stratification
        self.biomarker = biomarker


def records(**kinds):
    """Build a record map whose values pass `isinstance` against the real schema classes via monkeypatching."""
    return kinds


# --- citation reading ----------------------------------------------------------------------------


def test_cited_ids_merges_the_cites_field_and_the_inline_tags() -> None:
    s = section(text="as shown [[C-1]] and [[INT-4]]", cites=["C-1", "H-2"])
    assert cited_ids(s) == ["C-1", "H-2", "INT-4"]


def test_cited_ids_keeps_first_order_and_drops_duplicates() -> None:
    assert cited_ids(section(text="[[C-1]] [[C-1]]", cites=["C-1"])) == ["C-1"]


# --- B: therapeutic hypothesis -------------------------------------------------------------------


def test_hypothesis_section_needs_a_hypothesis_record(monkeypatch) -> None:
    import pipeline.section_gates as sg

    monkeypatch.setattr(sg, "Hypothesis", FakeHypothesis)
    monkeypatch.setattr(sg, "Experiment", FakeExperiment)
    unmet = check_therapeutic_hypothesis(section(cites=["EXP-1"]), {"EXP-1": FakeExperiment()})
    assert unmet == ["cites no H- hypothesis record"]


def test_hypothesis_section_needs_an_experiment_that_could_contradict(monkeypatch) -> None:
    """A hypothesis tested only by experiments that could confirm it is not falsifiable by what it cites."""
    import pipeline.section_gates as sg

    monkeypatch.setattr(sg, "Hypothesis", FakeHypothesis)
    monkeypatch.setattr(sg, "Experiment", FakeExperiment)
    kb = {"H-1": FakeHypothesis(), "EXP-1": FakeExperiment(effects=("supports", "supports"))}
    unmet = check_therapeutic_hypothesis(section(cites=["H-1", "EXP-1"]), kb)
    assert unmet == ["cites no experiment declaring an outcome that would contradict a claim"]


def test_hypothesis_section_passes_with_a_falsifier(monkeypatch) -> None:
    import pipeline.section_gates as sg

    monkeypatch.setattr(sg, "Hypothesis", FakeHypothesis)
    monkeypatch.setattr(sg, "Experiment", FakeExperiment)
    kb = {"H-1": FakeHypothesis(), "EXP-1": FakeExperiment()}
    assert check_therapeutic_hypothesis(section(cites=["H-1", "EXP-1"]), kb) == []


# --- D and E: stratification and biomarker -------------------------------------------------------


@pytest.mark.parametrize(
    "check,field,message",
    [
        (check_stratification, "stratification", "cites no INT- candidate carrying a stratification"),
        (check_biomarker, "biomarker", "cites no INT- candidate carrying a biomarker"),
    ],
)
def test_subgroup_and_biomarker_must_be_traceable_to_a_candidate(monkeypatch, check, field, message) -> None:
    import pipeline.section_gates as sg

    monkeypatch.setattr(sg, "Intervention", FakeIntervention)
    assert check(section(cites=["C-1"]), {"C-1": object()}) == [message]
    assert check(section(cites=["INT-1"]), {"INT-1": FakeIntervention()}) == []


def test_a_candidate_with_the_field_empty_does_not_satisfy_the_section(monkeypatch) -> None:
    """Citing a candidate is not enough; it must be one that states the thing the section claims."""
    import pipeline.section_gates as sg

    monkeypatch.setattr(sg, "Intervention", FakeIntervention)
    blank = FakeIntervention(stratification="", biomarker="")
    assert check_stratification(section(cites=["INT-1"]), {"INT-1": blank}) != []
    assert check_biomarker(section(cites=["INT-1"]), {"INT-1": blank}) != []


# --- G: translation ------------------------------------------------------------------------------


def test_translation_needs_more_than_one_experiment(monkeypatch) -> None:
    import pipeline.section_gates as sg

    monkeypatch.setattr(sg, "Experiment", FakeExperiment)
    unmet = check_translation(section(cites=["EXP-1"]), {"EXP-1": FakeExperiment()})
    assert unmet == ["cites fewer than two EXP- experiments, so it names no sequence of stages"]


def test_translation_of_a_single_kind_is_one_stage_not_a_path(monkeypatch) -> None:
    import pipeline.section_gates as sg

    monkeypatch.setattr(sg, "Experiment", FakeExperiment)
    kb = {"EXP-1": FakeExperiment(kind="wet_lab"), "EXP-2": FakeExperiment(kind="wet_lab")}
    unmet = check_translation(section(cites=["EXP-1", "EXP-2"]), kb)
    assert unmet == ["cites experiments of a single kind, so it names one stage rather than a path"]


def test_translation_passes_with_two_kinds(monkeypatch) -> None:
    import pipeline.section_gates as sg

    monkeypatch.setattr(sg, "Experiment", FakeExperiment)
    kb = {"EXP-1": FakeExperiment(kind="in_silico"), "EXP-2": FakeExperiment(kind="wet_lab")}
    assert check_translation(section(cites=["EXP-1", "EXP-2"]), kb) == []


# --- the registry --------------------------------------------------------------------------------


def test_the_registry_covers_a_b_d_e_and_g() -> None:
    """C, F and H are checked elsewhere by the records they rest on."""
    assert set(CHECKS) == {"mechanism", "therapeutic_hypothesis", "stratification", "biomarker", "translation"}


def test_each_registered_check_states_what_its_section_requires() -> None:
    for name, (requirement, check) in CHECKS.items():
        assert requirement and callable(check), name


class FakeOutput:
    def __init__(self, **sections) -> None:
        for key, value in sections.items():
            setattr(self, key, value)


def test_check_sections_reports_only_the_sections_that_fail(monkeypatch) -> None:
    import pipeline.section_gates as sg

    monkeypatch.setattr(sg, "Intervention", FakeIntervention)
    monkeypatch.setattr(sg, "Experiment", FakeExperiment)
    monkeypatch.setattr(sg, "Hypothesis", FakeHypothesis)
    kb = {
        "INT-1": FakeIntervention(),
        "H-1": FakeHypothesis(),
        "EXP-1": FakeExperiment(kind="in_silico"),
        "EXP-2": FakeExperiment(kind="wet_lab"),
    }
    output = FakeOutput(
        therapeutic_hypothesis=section(cites=["H-1", "EXP-1"]),
        stratification=section(cites=["INT-1"]),
        biomarker=section(cites=["C-9"]),  # fails
        translation=section(cites=["EXP-1", "EXP-2"]),
    )
    findings = sg.check_sections(output, kb)
    assert set(findings) == {"biomarker"}
    assert findings["biomarker"]["missing"] == ["cites no INT- candidate carrying a biomarker"]
    assert findings["biomarker"]["requires"].startswith("E.")


def test_check_sections_skips_a_section_the_output_does_not_have(monkeypatch) -> None:
    """The checks must not assume a schema they do not own."""
    import pipeline.section_gates as sg

    monkeypatch.setattr(sg, "Intervention", FakeIntervention)
    assert sg.check_sections(FakeOutput(stratification=section(cites=["INT-1"])), {"INT-1": FakeIntervention()}) == {}


# --- A: the mechanism must be a chain, and must touch something checked ---------------------------
# These use real claim records rather than a fake, because the check turns on `causal_fields()` and on
# `provenance_status`, and a fake that answered those would be testing itself.


def _causal(cid: str, cause: str, effect: str, verified: bool = False):
    from kb_factories import claim

    return claim(
        id=cid,
        content=f"{cause} affects {effect}.",
        cause_ref=cause,
        effect_ref=effect,
        context="in ALS",
        mechanism="by a stated route",
        origin="literature" if verified else "agent",
        provenance_status="verified" if verified else "unverified",
        verification_method="quote" if verified else "none",
        source_refs=["doi:10.1000/x"] if verified else [],
        quote="a passage" if verified else None,
    )


def test_a_mechanism_with_no_causal_claim_states_no_mechanism() -> None:
    from kb_factories import claim

    from pipeline.section_gates import check_mechanism

    kb = {"C-1": claim(id="C-1", content="A plain observation.")}
    assert check_mechanism(section(cites=["C-1"]), kb) == ["cites no causal claim, so it states no mechanism"]


def test_disconnected_proper_edges_are_a_list_not_a_chain() -> None:
    """Two proper edges that share no endpoint assert two causes; a mechanism is a route between them."""
    from pipeline.section_gates import check_mechanism

    kb = {"C-1": _causal("C-1", "N-1", "N-2", verified=True), "C-2": _causal("C-2", "N-3", "N-4", verified=True)}
    assert check_mechanism(section(cites=["C-1", "C-2"]), kb) == [
        "cites proper causal edges that do not join: a list of causes, not a chain"
    ]


def test_a_chain_of_unchecked_assertions_is_refused() -> None:
    """A mechanism section can cite many causal edges without any of them being proper."""
    from pipeline.section_gates import check_mechanism

    kb = {"C-1": _causal("C-1", "N-1", "N-2"), "C-2": _causal("C-2", "N-2", "N-3")}
    assert check_mechanism(section(cites=["C-1", "C-2"]), kb) == [
        "cites no proper causal edge (its quote verified against its source, or supported by a verified relation): "
        "every step of the chain is an unchecked assertion"
    ]


def test_two_unchecked_edges_beside_a_proper_one_are_refused() -> None:
    from pipeline.section_gates import check_mechanism

    kb = {
        "C-1": _causal("C-1", "N-1", "N-2", verified=True),
        "C-2": _causal("C-2", "N-2", "N-3"),
        "C-3": _causal("C-3", "N-3", "N-4"),
    }
    assert check_mechanism(section(cites=["C-1", "C-2", "C-3"]), kb) == [
        "cites 2 unchecked causal edges (C-2, C-3); at most one, the hypothesis step, is allowed"
    ]


def test_an_unchecked_edge_not_adjacent_to_the_proper_chain_is_refused() -> None:
    from pipeline.section_gates import check_mechanism

    kb = {"C-1": _causal("C-1", "N-1", "N-2", verified=True), "C-2": _causal("C-2", "N-7", "N-8")}
    assert check_mechanism(section(cites=["C-1", "C-2"]), kb) == [
        "its unchecked edge C-2 is not adjacent to the proper chain"
    ]


def test_a_proper_chain_with_one_unchecked_step_at_its_end_or_side_passes() -> None:
    from pipeline.section_gates import check_mechanism

    proper = {
        "C-1": _causal("C-1", "N-1", "N-2", verified=True),
        "C-2": _causal("C-2", "N-2", "N-3", verified=True),
    }
    at_end = proper | {"C-3": _causal("C-3", "N-3", "N-4")}
    on_side = proper | {"C-3": _causal("C-3", "N-2", "N-9")}
    assert check_mechanism(section(cites=["C-1", "C-2", "C-3"]), at_end) == []
    assert check_mechanism(section(cites=["C-1", "C-2", "C-3"]), on_side) == []


def test_an_edge_supported_by_a_verified_relation_counts_as_proper() -> None:
    from kb_factories import claim, relation

    from pipeline.section_gates import check_mechanism

    kb = {
        "C-1": _causal("C-1", "N-1", "N-2"),
        "C-90": claim(id="C-90", content="Verified evidence."),
        "L-1": relation(id="L-1", type="supports", source_id="C-90", target_id="C-1"),
    }
    assert check_mechanism(section(cites=["C-1"]), kb) == []


def test_a_contradicted_edge_is_named_and_does_not_count() -> None:
    from kb_factories import claim, relation

    from pipeline.section_gates import check_mechanism

    kb = {
        "C-1": _causal("C-1", "N-1", "N-2", verified=True),
        "C-90": claim(id="C-90", content="Verified evidence against."),
        "L-1": relation(id="L-1", type="contradicts", source_id="C-90", target_id="C-1"),
    }
    assert check_mechanism(section(cites=["C-1"]), kb) == [
        "cites causal claims that the mechanism view hides (contradicted, contested or superseded): C-1",
        "cites no proper causal edge (its quote verified against its source, or supported by a verified relation): "
        "every step of the chain is an unchecked assertion",
    ]


def test_a_joined_chain_touching_one_verified_edge_passes() -> None:
    from pipeline.section_gates import check_mechanism

    kb = {"C-1": _causal("C-1", "N-1", "N-2", verified=True), "C-2": _causal("C-2", "N-2", "N-3")}
    assert check_mechanism(section(cites=["C-1", "C-2"]), kb) == []
