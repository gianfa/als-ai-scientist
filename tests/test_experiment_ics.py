"""`ics_cytokine_skew_v1` on a synthetic deposit: FlowJo workspaces, deposited FCS names and a source-data sheet.

The fixture writes only what the analysis reads: workspace XML with the authors' gate counts, empty `.fcs` files
whose names carry group, well and condition, and an `.xlsx` holding the "ED Fig. 4" columns. One test runs on the
real deposit when it is present locally and is skipped otherwise.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from ics_fixtures import DONORS, deposit, expected_rows, source_data
from kb_factories import experiment

from analysis.experiment_ics_cytokines import ICS_RECORDS, mann_whitney_exact, read_deposit, responses, run
from analysis.experiment_runner import HANDLERS, execute
from pipeline.experiment_execution import check_interpretation, select_all
from pipeline.proposals import ExecuteOutput
from tools.data.local_sources import LocalSource, manifest

REAL = Path(__file__).resolve().parents[1] / "data" / "external"


@pytest.fixture
def inputs(tmp_path):
    cytometry = deposit(tmp_path)
    # Published rows: every donor of the first record, i.e. two per group; the other records reproduce nothing.
    xlsx = source_data(tmp_path / "source.xlsx", {g: expected_rows(g) for g in DONORS})
    return cytometry, xlsx


def test_each_donor_is_peptide_minus_vehicle_floored_and_rounded(inputs):
    donors = responses(read_deposit(inputs[0]))
    assert len(donors) == 4 * 4
    first = next(d for d in donors if d["record"] == ICS_RECORDS[0] and d["group"] == "ALS" and d["donor"] == "Donor 1")
    assert first["response"] == {"IFNg": 0.1, "IL-4": 1.2, "IL-10": 0.4, "IL-17": 0.01}
    assert first["skew_ratio"] == pytest.approx(0.1 / 1.6)


def test_donors_without_groups_are_consecutive_well_triplets(inputs):
    donors = {(d["record"], d["group"], d["donor"]) for d in responses(read_deposit(inputs[0]))}
    assert (ICS_RECORDS[1], "HC", "triplet-C1") in donors and (ICS_RECORDS[1], "HC", "triplet-C2") in donors


def test_a_donor_missing_a_well_is_refused(inputs):
    record_dir = inputs[0] / ICS_RECORDS[2]
    text = (record_dir / "ALS Donors.wsp").read_text()
    (record_dir / "ALS Donors.wsp").write_text(text.replace("Specimen_001_C3_C3.fcs", "Specimen_001_C9_C9.fcs"))
    (record_dir / "ALS Specimen_001_C9_C9 Positive.fcs").write_bytes(b"")
    with pytest.raises(ValueError, match="has wells"):
        responses(read_deposit(inputs[0]))


def test_a_workspace_sample_without_a_deposited_file_is_refused(inputs):
    (inputs[0] / ICS_RECORDS[1] / "HC Specimen_001_C2_C2 Peptide.fcs").unlink()
    with pytest.raises(ValueError, match="no deposited FCS file for HC well C2"):
        read_deposit(inputs[0])


def test_a_record_without_a_workspace_is_refused(inputs):
    for wsp in (inputs[0] / ICS_RECORDS[3]).glob("*.wsp"):
        wsp.unlink()
    with pytest.raises(ValueError, match="no FlowJo workspace"):
        read_deposit(inputs[0])


@pytest.mark.parametrize(
    "x, y, u, p",
    [
        ([1, 2, 3], [4, 5, 6], 0.0, 0.1),  # C(6,3) = 20 splits, the two most extreme are 2/20
        ([4, 5, 6], [1, 2, 3], 9.0, 0.1),
        ([1, 2, 3, 4], [1, 2, 3, 4], 8.0, 1.0),
    ],
)
def test_the_exact_mann_whitney(x, y, u, p):
    assert mann_whitney_exact(x, y) == (u, pytest.approx(p))


def test_the_rule_and_the_reproduction_are_reported_together(inputs):
    summary = run(*inputs)
    assert summary["groups"] == {"ALS": 8, "HC": 8}
    assert summary["outcome"] == "skew_replicated" and summary["p_value"] < 0.05
    # The published rows list the first record's donors once; identical donors of later records find none left.
    assert summary["reproduction"]["per_record"][ICS_RECORDS[0]] == {"donors": 4, "reproduced": 4}
    assert summary["reproduction"]["reproduced"] == 4
    assert "4 of 16 donors reproduce" in summary["conclusion"]


def test_an_equal_ratio_is_not_a_replicated_skew(tmp_path):
    cytometry = deposit(tmp_path, {"ALS": DONORS["HC"], "HC": DONORS["HC"]})
    summary = run(cytometry, source_data(tmp_path / "s.xlsx", {"HC": [], "ALS": []}))
    assert summary["outcome"] == "skew_not_replicated" and summary["negative_result"]


def test_the_runner_executes_it_on_manifest_bound_inputs_and_binds_the_interpretation(inputs, tmp_path):
    cytometry, xlsx = inputs
    data = manifest(
        [
            LocalSource("michaelis-zenodo-cytometry", cytometry, "raw", "fixture"),
            LocalSource("michaelis-source-data", xlsx, "table", "fixture"),
        ],
        tmp_path,
    )
    artifact = tmp_path / "artifact"
    summary = execute(data, tmp_path, artifact, "RUN-1", "EXP-1", "config", "ics_cytokine_skew_v1")
    assert (artifact / "verification.json").is_file()
    assert len(summary["inputs"]) == 4 * 2 + 1, "every workspace read and the source data, each hashed"
    assert execute(data, tmp_path, artifact, "RUN-1", "EXP-1", "config", "ics_cytokine_skew_v1") == summary

    selected = {"experiment_id": "EXP-1", "analysis_id": "ics_cytokine_skew_v1"}
    good = ExecuteOutput(
        experiment_id="EXP-1",
        analysis_id="ics_cytokine_skew_v1",
        result_digest=summary["result_digest"],
        outcome=summary["outcome"],
        interpretation="supports_ifng_skew_only",
        limitations=summary["limitations"],
    )
    assert check_interpretation(good, selected, artifact)["faithful"]
    borrowed = good.model_copy(update={"interpretation": "supports_panel_condition_only"})
    assert not check_interpretation(borrowed, selected, artifact)["faithful"]


def test_a_bought_ics_experiment_is_selected_with_exactly_its_two_inputs():
    from kb_factories import decision

    refs = [f"dataset:{sid}@abc" for sid in HANDLERS["ics_cytokine_skew_v1"].input_ids]
    good = experiment(
        id="EXP-2", kind="in_silico", dataset_location="local", dataset_refs=refs, analysis_id="ics_cytokine_skew_v1"
    )
    short = experiment(
        id="EXP-1",
        kind="in_silico",
        dataset_location="local",
        dataset_refs=refs[:1],
        analysis_id="ics_cytokine_skew_v1",
    )
    records = {r.id: r for r in (short, good, decision(experiment_ids=["EXP-1", "EXP-2"]))}
    selected, skipped = select_all(
        records, "RUN-1", {"EXP-1": {"status": "data_supported"}, "EXP-2": {"status": "data_supported"}}, 10
    )
    assert [row["experiment_id"] for row in selected] == ["EXP-2"]
    assert selected[0]["analysis_id"] == "ics_cytokine_skew_v1"
    assert skipped == [{"experiment_id": "EXP-1", "reason": "EXP does not name exactly the handler's inputs"}]


@pytest.mark.skipif(
    not (REAL / "michaelis-2025-zenodo" / ICS_RECORDS[0]).is_dir()
    or not (REAL / "michaelis-2025-nature-supp" / "41586_2025_9588_MOESM3_ESM.xlsx").is_file(),
    reason="the Michaelis deposit and source data are local-only inputs (docs/data-acquisition.md)",
)
def test_the_real_deposit_has_the_published_cohort_and_reproduces_screen_one_only():
    summary = run(
        REAL / "michaelis-2025-zenodo", REAL / "michaelis-2025-nature-supp" / "41586_2025_9588_MOESM3_ESM.xlsx"
    )
    assert summary["groups"] == {"ALS": 15, "HC": 15}, "Extended Data Fig. 4: n = 15 per group"
    per_record = summary["reproduction"]["per_record"]
    assert per_record["16749334"] == {"donors": 8, "reproduced": 7}
    assert all(per_record[r]["reproduced"] == 0 for r in ICS_RECORDS[1:])
    print(json.dumps({k: summary[k] for k in ("outcome", "p_value", "median_skew_ratio")}))
