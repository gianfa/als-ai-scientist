import json

import pytest

from analysis.datasets import sha256
from analysis.experiment_hla_panel import run
from analysis.experiment_runner import execute
from tools.experiment.mcp_server import run_experiment

PEPTIDES = ("AAAAAAAAAAAAAAA", "CCCCCCCCCCCCCCC", "GGGGGGGGGGGGGGG")
ALLELES = ("HLA-DRB1*01:01", "HLA-DRB1*03:01")


def fixture(tmp_path):
    table1 = tmp_path / "table1.csv"
    table1.write_text(
        '"**Peptide**\n**residue**",**Peptide sequence**,"**Response**\n**frequency (%)**","**Average**\n**magnitude**"\n'
        + "".join(f"{i * 15 + 1}–{i * 15 + 15},{p},1/2 (50%),1\n" for i, p in enumerate(PEPTIDES))
    )
    table2 = tmp_path / "table2.csv"
    table2.write_text(
        "**Peptide sequence**,**Allele**,**RATE**,**Binding percentile**\n"
        + f"{PEPTIDES[0]},DRB1*01:01,0.05,1\n"
        + f"{PEPTIDES[1]},DRB1*03:01,0.05,1\n"
    )
    cache = tmp_path / "cache.jsonl"
    ranks = ((1, 50), (50, 1), (50, 50))
    cache.write_text(
        "".join(
            json.dumps({"row": {"allele": allele, "peptide": peptide, "rank": rank}}) + "\n"
            for peptide, pair in zip(PEPTIDES, ranks)
            for allele, rank in zip(ALLELES, pair)
        )
    )
    files = dict(
        zip(("michaelis-table1-epitopes", "michaelis-table2-hla", "iedb-netmhciipan-cache"), (table1, table2, cache))
    )
    manifest = {
        "sources": [
            {"id": name, "path": str(path), "sha256": sha256(path), "ref": f"dataset:{name}@{sha256(path)[:12]}"}
            for name, path in files.items()
        ]
    }
    return table1, table2, cache, manifest


def test_panel_readout_and_exhaustive_control(tmp_path):
    table1, table2, cache, _ = fixture(tmp_path)
    result = run(table1, table2, cache)
    assert result["outcome"] == "panel_condition_met"
    assert result["selected_peptides"] == list(PEPTIDES[:2])
    assert result["covered_alleles"] == 2
    assert result["control_subsets"] == 3
    assert result["control_full_coverage_fraction"] == pytest.approx(1 / 3)


def test_incomplete_grid_is_not_a_scientific_result(tmp_path):
    table1, table2, cache, _ = fixture(tmp_path)
    cache.write_text("\n".join(cache.read_text().splitlines()[:-1]) + "\n")
    with pytest.raises(ValueError, match="prediction grid is incomplete"):
        run(table1, table2, cache)


def test_a_complete_grid_can_fail_the_panel_condition(tmp_path):
    table1, table2, cache, _ = fixture(tmp_path)
    records = [json.loads(line) for line in cache.read_text().splitlines()]
    for record in records:
        if record["row"]["allele"] == ALLELES[1]:
            record["row"]["rank"] = 50
    cache.write_text("".join(json.dumps(record) + "\n" for record in records))
    result = run(table1, table2, cache)
    assert result["outcome"] == "panel_condition_failed"
    assert result["covered_alleles"] == 1
    assert result["negative_result"] is True


def test_manifest_bound_artifact_reuses_only_a_verified_receipt(tmp_path):
    _, _, cache, manifest = fixture(tmp_path)
    output = tmp_path / "result"
    first = execute(manifest, tmp_path, output, "run-1", "EXP-1", "config-1", "hla_panel_compact_v1")
    assert first["outcome"] == "panel_condition_met"
    assert (output / "verification.json").is_file()
    assert execute(manifest, tmp_path, output, "run-1", "EXP-1", "config-1", "hla_panel_compact_v1") == first
    with pytest.raises(ValueError, match="does not match this request"):
        execute(manifest, tmp_path, output, "run-1", "EXP-2", "config-1", "hla_panel_compact_v1")
    receipt = json.loads((output / "verification.json").read_text())
    receipt["result_digest"] = "changed"
    (output / "verification.json").write_text(json.dumps(receipt))
    with pytest.raises(ValueError, match="verification receipt digest mismatch"):
        execute(manifest, tmp_path, output, "run-1", "EXP-1", "config-1", "hla_panel_compact_v1")
    cache.write_text(cache.read_text() + "\n")
    with pytest.raises(ValueError, match="manifest hash mismatch"):
        execute(manifest, tmp_path, tmp_path / "other", "run-1", "EXP-1", "config-1", "hla_panel_compact_v1")


def test_tool_refuses_unselected_experiment_and_returns_only_bounded_result(tmp_path):
    _, _, _, manifest = fixture(tmp_path)
    (tmp_path / "data-manifest.json").write_text(json.dumps(manifest))
    policy = tmp_path / "execution-policy.json"
    policy.write_text(
        json.dumps(
            {
                "run_id": "run-1",
                "config_hash": "config-1",
                "repository_root": str(tmp_path),
                "selected": [
                    {"experiment_id": "EXP-1", "analysis_id": "hla_panel_compact_v1", "feasibility": "data_supported"}
                ],
            }
        )
    )
    refused = run_experiment(policy, "EXP-2")
    assert "EXP-1" in refused["error"]  # the refusal names what this phase does authorize
    assert not (tmp_path / "experiment-analysis").exists()
    assert not (tmp_path / "experiment-analysis").exists()
    result = run_experiment(policy, "EXP-1")
    assert result["outcome"] == "panel_condition_met"
    assert result["readout"]["covered_alleles"] == 2
    assert result["receipt_ref"] == "experiment-analysis/EXP-1/verification.json"
    assert result == run_experiment(policy, "EXP-1")
