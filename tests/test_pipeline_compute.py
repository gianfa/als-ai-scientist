"""The `compute` phase: which claims a verified analysis artifact licenses, and what stops it."""

from __future__ import annotations

import json

import pytest

from pipeline.compute import (
    ANALYSIS_SOURCES,
    AnalysisFailed,
    claims_of,
    computation_ref,
    run_all,
    run_one,
)

SUMMARY = {
    "result_digest": "a" * 64,
    "negative_result": True,
    "conclusion": "Table 1 alone does not identify a single immunodominant epitope.",
    "results": [
        {"key": "primary_comparisons", "value": 20, "n": 23, "test": "Fisher exact + Holm"},
        {"key": "rank_one_probability", "value": 0.3456, "n": 10000, "test": "binomial bootstrap"},
    ],
}
DATASET_REF = "dataset:michaelis-table1-epitopes@89af13c3630a"


def test_each_numeric_result_becomes_one_quantitative_claim():
    claims = claims_of("a1_immunodominance", SUMMARY, [DATASET_REF])
    quantitative = [c for c in claims if c.type == "quantitative"]
    assert [c.content for c in quantitative] == [
        "a1_immunodominance: primary_comparisons = 20; n = 23; Fisher exact + Holm.",
        "a1_immunodominance: rank_one_probability = 0.3456; n = 10000; binomial bootstrap.",
    ]
    # Both the dataset version and the computation, so a changed input yields a different claim.
    assert quantitative[0].source_refs == [DATASET_REF, computation_ref("a1_immunodominance", SUMMARY)]


def test_a_negative_conclusion_is_kept_and_named_as_negative():
    [conclusion] = [c for c in claims_of("a1_immunodominance", SUMMARY, [DATASET_REF]) if c.type == "observational"]
    assert conclusion.content.startswith("Negative result of a1_immunodominance: Table 1 alone does not")
    positive = claims_of("a3_gwas_loci", {**SUMMARY, "negative_result": False}, [DATASET_REF])
    assert [c for c in positive if c.type == "observational"][0].content.startswith("Result of a3_gwas_loci:")


def test_an_artifact_without_results_or_conclusion_licenses_nothing():
    assert claims_of("a2_epitope_clustering", {"result_digest": "b" * 64}, [DATASET_REF]) == []


def test_an_unknown_analysis_is_refused_before_anything_runs(tmp_path):
    with pytest.raises(AnalysisFailed, match="unknown analysis 'a9_made_up'"):
        run_one("a9_made_up", {"sources": []}, tmp_path, tmp_path / "out", 1, "RUN-1", "hash")


def test_an_analysis_whose_source_the_manifest_lacks_fails_loudly(tmp_path):
    with pytest.raises(AnalysisFailed, match="a1_immunodominance: the manifest holds no source"):
        run_one("a1_immunodominance", {"sources": []}, tmp_path, tmp_path / "out", 1, "RUN-1", "hash")


def test_every_declared_analysis_names_the_sources_it_consumes():
    # A new analysis must declare its inputs here, or the phase cannot hash what it read.
    assert set(ANALYSIS_SOURCES) == {"a1_immunodominance", "a2_epitope_clustering", "a3_gwas_loci", "a4_hla_binding"}
    # A4's third input is the prediction cache acquired once over the network: it is an input, so the
    # phase hashes it and the artifact states which predictions it was computed over.
    assert "iedb-netmhciipan-cache" in ANALYSIS_SOURCES["a4_hla_binding"]
    assert all(sources for sources in ANALYSIS_SOURCES.values())


def test_run_all_reports_each_analysis_and_counts_its_claims(tmp_path):
    manifest = {"sources": [{"id": "michaelis-table1-epitopes", "ref": DATASET_REF}]}

    def runner(analysis_id, data, root, output_root, seed, run_id, config_hash):
        return SUMMARY, claims_of(analysis_id, SUMMARY, [DATASET_REF])

    claims, report = run_all(
        ["a1_immunodominance", "a2_epitope_clustering"],
        manifest,
        tmp_path,
        tmp_path / "run" / "analysis",
        1,
        "RUN-1",
        "hash",
        runner=runner,
    )

    assert len(claims) == 6  # 2 numeric + 1 conclusion, twice
    assert report["a1_immunodominance"]["claims"] == 3
    assert report["a1_immunodominance"]["results"] == {"primary_comparisons": 20, "rank_one_probability": 0.3456}
    assert report["a2_epitope_clustering"]["negative_result"] is True
    assert report["a1_immunodominance"]["artifact"] == "analysis/a1_immunodominance/summary.json"


def test_a_failing_analysis_stops_the_phase(tmp_path):
    def runner(analysis_id, *args):
        if analysis_id == "a2_epitope_clustering":
            raise AnalysisFailed("a2_epitope_clustering: ValueError: schema drift")
        return SUMMARY, []

    with pytest.raises(AnalysisFailed, match="schema drift"):
        run_all(
            ["a1_immunodominance", "a2_epitope_clustering", "a3_gwas_loci"],
            {"sources": []},
            tmp_path,
            tmp_path,
            1,
            "RUN-1",
            "hash",
            runner=runner,
        )


def test_an_existing_artifact_is_reused_only_if_its_digest_verifies(tmp_path):
    """A re-attempt must not recompute over an earlier result, and must not trust a tampered one."""
    from analysis.verify import digest

    manifest = {
        "sources": [{"id": "als-gwas-gcst90027164", "ref": DATASET_REF, "path": str(tmp_path / "sumstats.tsv.gz")}]
    }
    output = tmp_path / "out" / "a3_gwas_loci"
    output.mkdir(parents=True)
    summary = {
        "analysis_id": "a3_gwas_loci",
        "conclusion": "lambda is 1.05",
        "results": [],
        "inputs": [],
        "artifacts": [],
    }
    summary["result_digest"] = digest(summary)
    (output / "summary.json").write_text(json.dumps(summary))

    reused, claims = run_one("a3_gwas_loci", manifest, tmp_path, tmp_path / "out", 1, "RUN-1", "hash")
    assert reused["result_digest"] == summary["result_digest"] and len(claims) == 1

    (output / "summary.json").write_text(json.dumps({**summary, "conclusion": "lambda is 2.00"}))
    with pytest.raises(AnalysisFailed, match="digest"):
        run_one("a3_gwas_loci", manifest, tmp_path, tmp_path / "out", 1, "RUN-1", "hash")


def test_the_computation_reference_ignores_which_run_produced_the_artifact():
    """Two runs of the same code on the same inputs must cite the same computation."""
    first = {
        **SUMMARY,
        "provenance": {"run_id": "RUN-1", "python_version": "3.10.0"},
        "git_commit": "abc",
        "result_digest": "1" * 64,
    }
    second = {
        **SUMMARY,
        "provenance": {"run_id": "RUN-2", "python_version": "3.10.0"},
        "git_commit": "def",
        "result_digest": "2" * 64,
    }
    assert computation_ref("a1_immunodominance", first) == computation_ref("a1_immunodominance", second)
    changed = {**first, "results": [{"key": "p_value", "value": 0.9, "n": 1, "test": "t"}]}
    assert computation_ref("a1_immunodominance", changed) != computation_ref("a1_immunodominance", first)
