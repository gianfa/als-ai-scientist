import json
from pathlib import Path

import pytest
from kb_factories import decision, experiment
from test_experiment_runner import fixture

from analysis.experiment_runner import HANDLERS, execute
from pipeline.experiment_execution import check_interpretation, select_all
from pipeline.proposals import ExecuteOutput

INPUT_IDS = HANDLERS["hla_panel_compact_v1"].input_ids
ICS_INPUT_IDS = HANDLERS["ics_cytokine_skew_v1"].input_ids
CAP = 10  # above anything these cases buy, so only the test about the cap is about the cap


def registered(**overrides):
    fields = {
        "kind": "in_silico",
        "dataset_location": "local",
        "dataset_refs": [f"dataset:{sid}@abc" for sid in INPUT_IDS],
        "analysis_id": "hla_panel_compact_v1",
    }
    return experiment(**(fields | overrides))


def ics(**overrides):
    fields = {
        "kind": "in_silico",
        "dataset_location": "local",
        "dataset_refs": [f"dataset:{sid}@abc" for sid in ICS_INPUT_IDS],
        "analysis_id": "ics_cytokine_skew_v1",
    }
    return experiment(**(fields | overrides))


def test_only_a_bought_supported_registered_experiment_is_selected():
    wrong = experiment(id="EXP-1")
    good = registered(id="EXP-2")
    records = {r.id: r for r in (wrong, good, decision(experiment_ids=["EXP-1", "EXP-2"]))}
    selected, skipped = select_all(
        records, "RUN-1", {"EXP-1": {"status": "not_applicable"}, "EXP-2": {"status": "data_supported"}}, CAP
    )
    assert [row["experiment_id"] for row in selected] == ["EXP-2"]
    assert selected[0]["tested_claims"] == ["C-1"]
    assert skipped == [{"experiment_id": "EXP-1", "reason": "no registered execution handler"}]


def test_unbought_or_unverified_experiment_does_not_execute():
    good = registered()
    records = {good.id: good, "D-1": decision()}
    selected, skipped = select_all(records, "RUN-1", {"EXP-1": {"status": "unverified", "reason": "no inventory"}}, CAP)
    assert selected == []
    assert "no inventory" in skipped[0]["reason"]
    selected, skipped = select_all({good.id: good}, "RUN-1", {"EXP-1": {"status": "data_supported"}}, CAP)
    assert selected == [] and skipped == []


def test_every_eligible_bought_experiment_is_selected_not_only_the_first():
    """A run with two hypotheses, two handlers, four eligible bought EXPs and one executed experiment.

    `select()` returned on the first survivor, so `EXP-2`, `EXP-4` and `EXP-5` were never examined and the
    second registered handler never ran in production.
    """
    records = {
        r.id: r
        for r in (
            registered(id="EXP-1", hypothesis_id="H-1"),
            ics(id="EXP-2", hypothesis_id="H-1"),
            ics(id="EXP-4", hypothesis_id="H-2"),
            registered(id="EXP-5", hypothesis_id="H-2"),
            decision(id="D-1", hypothesis_id="H-1", experiment_ids=["EXP-1", "EXP-2"]),
            decision(id="D-2", hypothesis_id="H-2", experiment_ids=["EXP-4", "EXP-5"]),
        )
    }
    feasibility = {eid: {"status": "data_supported"} for eid in ("EXP-1", "EXP-2", "EXP-4", "EXP-5")}
    selected, skipped = select_all(records, "RUN-1", feasibility, CAP)
    assert [row["experiment_id"] for row in selected] == ["EXP-1", "EXP-2", "EXP-4", "EXP-5"]
    assert sorted({row["analysis_id"] for row in selected}) == ["hla_panel_compact_v1", "ics_cytokine_skew_v1"]
    assert skipped == []


def test_an_ineligible_bought_experiment_after_an_eligible_one_still_keeps_its_reason():
    """A run recorded `"skipped": []` while three bought EXPs were dropped: the loop never reached them."""
    records = {
        r.id: r for r in (registered(id="EXP-1"), experiment(id="EXP-2"), decision(experiment_ids=["EXP-1", "EXP-2"]))
    }
    selected, skipped = select_all(
        records, "RUN-1", {"EXP-1": {"status": "data_supported"}, "EXP-2": {"status": "not_applicable"}}, CAP
    )
    assert [row["experiment_id"] for row in selected] == ["EXP-1"]
    assert skipped == [{"experiment_id": "EXP-2", "reason": "no registered execution handler"}]


def test_the_execution_cap_records_what_it_excluded():
    # Two different computations: the same one bought twice would be a reuse, which the cap does not count.
    records = {
        r.id: r
        for r in (
            registered(id="EXP-1", hypothesis_id="H-1"),
            ics(id="EXP-2", hypothesis_id="H-2"),
            decision(experiment_ids=["EXP-1", "EXP-2"]),
        )
    }
    feasibility = {"EXP-1": {"status": "data_supported"}, "EXP-2": {"status": "data_supported"}}
    selected, skipped = select_all(records, "RUN-1", feasibility, 1)
    assert [row["experiment_id"] for row in selected] == ["EXP-1"]
    assert skipped == [{"experiment_id": "EXP-2", "reason": "the run's execution cap of 1 experiments was reached"}]


def test_the_same_hypothesis_does_not_run_the_same_analysis_on_the_same_inputs_twice():
    """Deterministic handler, same inputs, same hypothesis: the second EXP would add nothing.

    A *different* hypothesis buying the same analysis is not excluded — that is the case where the
    result is already known but the hypothesis waiting on it is new — but it reuses the result.
    """
    records = {
        r.id: r
        for r in (
            registered(id="EXP-1", hypothesis_id="H-1"),
            registered(id="EXP-2", hypothesis_id="H-1"),
            registered(id="EXP-3", hypothesis_id="H-2"),
            decision(experiment_ids=["EXP-1", "EXP-2", "EXP-3"]),
        )
    }
    feasibility = {eid: {"status": "data_supported"} for eid in ("EXP-1", "EXP-2", "EXP-3")}
    selected, skipped = select_all(records, "RUN-1", feasibility, CAP)
    assert [row["experiment_id"] for row in selected] == ["EXP-1", "EXP-3"]
    assert selected[1]["result_of"] == "EXP-1"
    assert skipped == [
        {"experiment_id": "EXP-2", "reason": "an EXP of this hypothesis already runs this analysis on these inputs"}
    ]


def test_one_analysis_on_the_same_inputs_is_computed_once_whoever_bought_it():
    """Three hypotheses, one analysis, one pair of inputs, three identical runs."""
    records = {
        r.id: r
        for r in (
            ics(id="EXP-1", hypothesis_id="H-1"),
            ics(id="EXP-4", hypothesis_id="H-2"),
            ics(id="EXP-7", hypothesis_id="H-3"),
            ics(id="EXP-8", hypothesis_id="H-2"),
            decision(experiment_ids=["EXP-1", "EXP-4", "EXP-7", "EXP-8"]),
        )
    }
    feasibility = {eid: {"status": "data_supported"} for eid in ("EXP-1", "EXP-4", "EXP-7", "EXP-8")}
    selected, skipped = select_all(records, "RUN-1", feasibility, CAP)
    assert [(row["experiment_id"], row.get("result_of")) for row in selected] == [
        ("EXP-1", None),
        ("EXP-4", "EXP-1"),
        ("EXP-7", "EXP-1"),
    ]
    assert selected[1]["selection_reason"] == "reuses the verified result of EXP-1: same analysis on the same inputs"
    # A second EXP of a hypothesis already bound to the computation adds nothing, reuse or not.
    assert skipped == [
        {"experiment_id": "EXP-8", "reason": "an EXP of this hypothesis already runs this analysis on these inputs"}
    ]


def test_the_same_analysis_on_different_inputs_is_two_computations():
    records = {
        r.id: r
        for r in (
            ics(id="EXP-1", hypothesis_id="H-1"),
            ics(id="EXP-2", hypothesis_id="H-2"),
            decision(experiment_ids=["EXP-1", "EXP-2"]),
        )
    }
    feasibility = {
        "EXP-1": {"status": "data_supported", "sources": [{"sha256": "a"}, {"sha256": "b"}]},
        "EXP-2": {"status": "data_supported", "sources": [{"sha256": "a"}, {"sha256": "c"}]},
    }
    selected, _ = select_all(records, "RUN-1", feasibility, CAP)
    assert [row.get("result_of") for row in selected] == [None, None]
    feasibility["EXP-2"]["sources"] = [{"sha256": "b"}, {"sha256": "a"}]  # order is not identity
    selected, _ = select_all(records, "RUN-1", feasibility, CAP)
    assert [row.get("result_of") for row in selected] == [None, "EXP-1"]


def test_the_cap_counts_computations_not_reuses():
    records = {
        r.id: r
        for r in (
            ics(id="EXP-1", hypothesis_id="H-1"),
            ics(id="EXP-2", hypothesis_id="H-2"),
            registered(id="EXP-3", hypothesis_id="H-3"),
            decision(experiment_ids=["EXP-1", "EXP-2", "EXP-3"]),
        )
    }
    feasibility = {eid: {"status": "data_supported"} for eid in ("EXP-1", "EXP-2", "EXP-3")}
    selected, skipped = select_all(records, "RUN-1", feasibility, 1)
    assert [(row["experiment_id"], row.get("result_of")) for row in selected] == [("EXP-1", None), ("EXP-2", "EXP-1")]
    assert skipped == [{"experiment_id": "EXP-3", "reason": "the run's execution cap of 1 experiments was reached"}]


def test_agent_cannot_substitute_a_digest_or_a_stronger_interpretation(tmp_path):
    _, _, _, manifest = fixture(tmp_path)
    artifact = tmp_path / "artifact"
    summary = execute(manifest, tmp_path, artifact, "RUN-1", "EXP-1", "config", "hla_panel_compact_v1")
    selected = {"experiment_id": "EXP-1"}
    good = ExecuteOutput(
        experiment_id="EXP-1",
        analysis_id="hla_panel_compact_v1",
        result_digest=summary["result_digest"],
        outcome=summary["outcome"],
        interpretation="supports_panel_condition_only",
        limitations=summary["limitations"],
    )
    assert check_interpretation(good, selected, artifact)["faithful"]
    forged = good.model_copy(update={"result_digest": "f" * 64, "interpretation": "weakens_panel_condition_only"})
    check = check_interpretation(forged, selected, artifact)
    assert not check["faithful"]
    assert len(check["errors"]) == 2


# A replay of a real archived run, read-only, with no paid session. It is the one
# measurement that contradicts what that run recorded — `[EXP-1]` and `skipped: []`.
ARCHIVE = Path(__file__).resolve().parents[1] / "experiments" / "pl-prod-010"


@pytest.mark.skipif(not (ARCHIVE / "kb").is_dir(), reason="the pl-prod-010 archive is not in this checkout")
def test_pl_prod_010_would_now_select_all_four_bought_experiments():
    from knowledge_base.store import read_records

    records = read_records(ARCHIVE / "kb")
    feasibility = json.loads(next((ARCHIVE / "results").glob("*-design-feasibility.json")).read_text())
    selected, skipped = select_all(records, "pl-prod-010", feasibility["experiments"], CAP)
    assert [row["experiment_id"] for row in selected] == ["EXP-1", "EXP-2", "EXP-4", "EXP-5"]
    assert sorted({row["analysis_id"] for row in selected}) == ["hla_panel_compact_v1", "ics_cytokine_skew_v1"]
    # The two wet-lab EXPs the run also bought are excluded, and now say why — that run's report said nothing.
    assert [row["experiment_id"] for row in skipped] == ["EXP-7", "EXP-9"]
    assert {row["reason"] for row in skipped} == {"no registered execution handler"}
    archived = json.loads((ARCHIVE / "results" / "09-execute-report.json").read_text())
    assert archived["selected"]["experiment_id"] == "EXP-1" and archived["skipped"] == []


ARCHIVE_011 = Path(__file__).resolve().parents[1] / "experiments" / "pl-prod-011"


@pytest.mark.skipif(not (ARCHIVE_011 / "kb").is_dir(), reason="the pl-prod-011 archive is not in this checkout")
def test_a_replayed_archive_computes_its_one_analysis_once():
    """The archive ran `ics_cytokine_skew_v1` three times for three hypotheses; replayed read-only, it is one
    computation and two reuses."""
    from knowledge_base.store import read_records

    records = read_records(ARCHIVE_011 / "kb")
    feasibility = json.loads((ARCHIVE_011 / "results" / "07-design-feasibility.json").read_text())
    selected, _ = select_all(records, "pl-prod-011", feasibility["experiments"], CAP)
    assert [(row["experiment_id"], row.get("result_of")) for row in selected] == [
        ("EXP-1", None),
        ("EXP-4", "EXP-1"),
        ("EXP-7", "EXP-1"),
    ]
    archived = json.loads((ARCHIVE_011 / "results" / "09-execute-report.json").read_text())
    assert archived["executed_count"] == 3 and "reused_count" not in archived
