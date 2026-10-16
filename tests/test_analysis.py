import json
from pathlib import Path

import pytest

from analysis.a1_immunodominance import run
from analysis.a2_epitope_clustering import exact_run_distribution
from analysis.a2_epitope_clustering import run as run_a2
from analysis.datasets import Epitope, load_table1
from analysis.runner import run_analysis
from analysis.stats import binom_cdf, clopper_pearson, fisher_exact_2x2, holm
from analysis.verify import verify_receipt, verify_run, verify_summary


def rows():
    return [
        Epitope("41–55", 41, "A" * 15, 3, 22, 14, False),
        Epitope("56–70", 56, "B" * 15, 7, 22, 32, False),
        Epitope("61–75", 61, "C" * 15, 3, 22, 14, False),
        Epitope("426–440<sup>a</sup>", 426, "D" * 15, 5, 19, 26, True),
    ]


def test_exact_statistics_are_deterministic():
    assert binom_cdf(10, 10, 1) == 1
    assert fisher_exact_2x2(1, 9, 11, 3) == fisher_exact_2x2(1, 9, 11, 3)
    assert clopper_pearson(0, 10)[0] == 0
    assert holm([0.01, 0.02])[0] <= holm([0.01, 0.02])[1]


def test_a1_preserves_phospho_rows_and_reports_negative_result():
    result = run(rows(), seed=7, bootstrap_replicates=100)
    assert len(result["table1_rows"]) == 4
    assert result["table1_rows"][-1]["phospho"] is True
    assert result["negative_result"] is True


def test_a2_exact_distribution_and_seeded_monte_carlo():
    assert exact_run_distribution(4, 2) == {1: 3, 2: 3}
    first = run_a2([1, 6, 11, 16], grid_size=10, replicates=500, seed=4)
    second = run_a2([1, 6, 11, 16], grid_size=10, replicates=500, seed=4)
    assert first == second
    assert first["observed_runs"] == 1
    assert 0 <= first["p_value"] <= 1


def _table1(tmp_path, header):
    path = tmp_path / "t1.csv"
    path.write_text(header + "\n41–55,ACDEFGHIKLMNPQR,3/10 (30%),120\n", encoding="utf-8")
    return path


def test_table1_schema_check_ignores_the_extractions_markup_but_not_the_columns(tmp_path):
    """Two Paper2Agent extractions can write the headers as plain text or as bold markdown with line breaks.
    Formatting must not matter; the columns must."""
    first = (
        '"**Peptide**\n**residue**",**Peptide sequence**,"**Response**\n**frequency (%)**","**Average**\n**magnitude**"'
    )
    second = "Peptide residue,Peptide sequence,Response frequency (%),Average magnitude"
    assert load_table1(_table1(tmp_path, first)) == load_table1(_table1(tmp_path, second))
    with pytest.raises(ValueError, match="schema drift"):
        load_table1(_table1(tmp_path, "Peptide residue,Allele,Response frequency (%),Average magnitude"))


@pytest.mark.parametrize("mark", ["<sup>a</sup>", "\u1d43"])
def test_the_phosphorylation_footnote_is_read_in_either_form_and_kept_out_of_the_sequence(tmp_path, mark):
    """An extraction can write Table 1's footnote "a" as `<sup>a</sup>` or as the Unicode modifier letter. The
    mark is the `phospho` flag and in two rows it sits inside the sequence, so it must be read, then removed
    from the peptide."""
    path = tmp_path / "t1.csv"
    path.write_text(
        f"Peptide residue,Peptide sequence,Response frequency (%),Average magnitude\n"
        f"431–445{mark},RNLKIDLDLT{mark}AEGDL,5/19 (26%),718\n",
        encoding="utf-8",
    )
    (row,) = load_table1(path)
    assert row.phospho is True
    assert row.sequence == "RNLKIDLDLTAEGDL"  # the marker never stays in the peptide
    assert row.residue_range == "431–445<sup>a</sup>"  # one label whichever form was extracted
    assert (row.start, row.k, row.n) == (431, 5, 19)


def test_the_residue_label_is_the_same_whichever_form_the_footnote_was_extracted_in(tmp_path):
    """The label becomes A1's `epitope` field, so two extractions of one PDF must not differ in it."""
    labels = []
    for mark in ("<sup>a</sup>", "ᵃ"):
        path = tmp_path / f"t1-{len(labels)}.csv"
        path.write_text(
            f"Peptide residue,Peptide sequence,Response frequency (%),Average magnitude\n"
            f"431–445{mark},RNLKIDLDLT{mark}AEGDL,5/19 (26%),718\n",
            encoding="utf-8",
        )
        labels.append(load_table1(path)[0].residue_range)
    assert labels[0] == labels[1]


def test_a_non_phosphorylated_residue_label_is_unchanged(tmp_path):
    path = tmp_path / "t1.csv"
    path.write_text(
        "Peptide residue,Peptide sequence,Response frequency (%),Average magnitude\n"
        "426–440,AAAAAAAAAAAAAAA,1/19 (5%),10\n",
        encoding="utf-8",
    )
    (row,) = load_table1(path)
    assert (row.residue_range, row.phospho) == ("426–440", False)


def test_a_sequence_with_a_character_that_is_not_an_amino_acid_is_refused(tmp_path):
    """If a future extraction writes the footnote some third way, the peptide must fail loudly rather than carry
    the stray character into a result."""
    path = tmp_path / "t1.csv"
    path.write_text(
        "Peptide residue,Peptide sequence,Response frequency (%),Average magnitude\n431–445,RNLKIDLDLT*AEGDL,5/19 (26%),718\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="malformed peptide sequence"):
        load_table1(path)


def test_paper2agent_table_parses_when_available():
    source = (
        Path(__file__).parents[1]
        / "dist/michaelis-2025-agent/skill/michaelis-2025-paper/assets/table/table-p0006-001.csv"
    )
    if not source.exists():
        pytest.skip("paper2agent output is not available in this checkout")
    assert len(load_table1(source)) == 23


def test_runner_verifies_and_refuses_overwrite(tmp_path):
    source = tmp_path / "paper2agent"
    (source / "assets/table").mkdir(parents=True)
    table1 = (
        Path(__file__).parents[1]
        / "dist/michaelis-2025-agent/skill/michaelis-2025-paper/assets/table/table-p0006-001.csv"
    )
    table2 = (
        Path(__file__).parents[1]
        / "dist/michaelis-2025-agent/skill/michaelis-2025-paper/assets/table/table-p0006-007.csv"
    )
    if not table1.exists():
        pytest.skip("paper2agent output is not available in this checkout")
    (source / "assets/table/table-p0006-001.csv").write_bytes(table1.read_bytes())
    (source / "assets/table/table-p0006-007.csv").write_bytes(table2.read_bytes())
    output = tmp_path / "out"
    run_analysis("a1_immunodominance", source, output, seed=2)
    assert verify_summary(output / "summary.json") is True
    receipt_path = output / "verification.json"
    receipt = verify_run(output / "summary.json", receipt_path)
    assert receipt["verified"] is True
    assert receipt["checks"] == {"result_digest": True, "inputs": True, "artifacts": True}
    assert verify_receipt(receipt_path) is True
    with pytest.raises(FileExistsError):
        verify_run(output / "summary.json", receipt_path)
    with pytest.raises(FileExistsError):
        run_analysis("a1_immunodominance", source, output, seed=2)
    summary = json.loads((output / "summary.json").read_text())
    summary["conclusion"] = "tampered"
    (output / "summary.json").write_text(json.dumps(summary))
    with pytest.raises(ValueError, match="digest mismatch"):
        verify_summary(output / "summary.json")


def test_verification_receipt_digest_and_binding_are_checked(tmp_path):
    import hashlib

    from analysis.verify import digest

    input_path = tmp_path / "input.csv"
    artifact_path = tmp_path / "artifact.csv"
    input_path.write_text("source\n")
    artifact_path.write_text("result\n")
    input_hash = hashlib.sha256(input_path.read_bytes()).hexdigest()
    artifact_hash = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    summary = {
        "analysis_id": "a1_immunodominance",
        "inputs": [{"path": str(input_path), "sha256": input_hash}],
        "artifacts": [{"path": "artifact.csv", "sha256": artifact_hash}],
    }
    summary["result_digest"] = digest(summary)
    summary_path = tmp_path / "summary.json"
    summary_path.write_text(json.dumps(summary))
    receipt = {
        "verification_version": "1",
        "analysis_id": summary["analysis_id"],
        "result_digest": summary["result_digest"],
        "summary_path": str(summary_path),
        "verified": True,
        "checks": {"result_digest": True, "inputs": True, "artifacts": True},
        "details": {
            "inputs": [{"path": str(input_path), "sha256": input_hash, "verified": True}],
            "artifacts": [{"path": "artifact.csv", "sha256": artifact_hash, "verified": True}],
        },
    }
    receipt["verification_digest"] = hashlib.sha256(
        json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    receipt_path = tmp_path / "verification.json"
    receipt_path.write_text(json.dumps(receipt))
    assert verify_receipt(receipt_path) is True
    receipt["checks"]["inputs"] = False
    receipt_path.write_text(json.dumps(receipt))
    with pytest.raises(ValueError, match="receipt digest mismatch"):
        verify_receipt(receipt_path)


@pytest.mark.parametrize("target", ["input", "artifact"])
@pytest.mark.parametrize("action", ["mutate", "delete"])
def test_verification_receipt_reopens_recorded_files(tmp_path, target, action):
    import hashlib

    from analysis.verify import digest

    input_path = tmp_path / "input.csv"
    artifact_path = tmp_path / "artifact.csv"
    input_path.write_text("source\n")
    artifact_path.write_text("result\n")
    input_hash = hashlib.sha256(input_path.read_bytes()).hexdigest()
    artifact_hash = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    summary = {
        "analysis_id": "a1_immunodominance",
        "inputs": [{"path": str(input_path), "sha256": input_hash}],
        "artifacts": [{"path": "artifact.csv", "sha256": artifact_hash}],
    }
    summary["result_digest"] = digest(summary)
    summary_path = tmp_path / "summary.json"
    summary_path.write_text(json.dumps(summary))
    receipt = {
        "verification_version": "1",
        "analysis_id": summary["analysis_id"],
        "result_digest": summary["result_digest"],
        "summary_path": str(summary_path),
        "verified": True,
        "checks": {"result_digest": True, "inputs": True, "artifacts": True},
        "details": {
            "inputs": [{"path": str(input_path), "sha256": input_hash, "verified": True}],
            "artifacts": [{"path": "artifact.csv", "sha256": artifact_hash, "verified": True}],
        },
    }
    receipt["verification_digest"] = hashlib.sha256(
        json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    receipt_path = tmp_path / "verification.json"
    receipt_path.write_text(json.dumps(receipt))
    target_path = input_path if target == "input" else artifact_path
    if action == "mutate":
        target_path.write_text("changed\n")
    else:
        target_path.unlink()
    with pytest.raises(ValueError, match="(hash mismatch|file is missing)"):
        verify_receipt(receipt_path)


def test_verify_run_requires_non_empty_inputs_and_artifacts(tmp_path):
    from analysis.verify import digest

    summary = {"analysis_id": "a1_immunodominance", "inputs": [], "artifacts": []}
    summary["result_digest"] = digest(summary)
    summary_path = tmp_path / "summary.json"
    summary_path.write_text(json.dumps(summary))
    with pytest.raises(ValueError, match="no inputs"):
        verify_run(summary_path)
