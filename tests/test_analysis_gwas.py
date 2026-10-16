import gzip

import pytest

from analysis.a3_gwas_loci import run
from analysis.runner import run_gwas_analysis
from analysis.verify import verify_run

HEADER = "rsid\tchromosome\tbase_pair_location\teffect_allele\tother_allele\teffect_allele_frequency\tbeta\tstandard_error\tp_value\tN_effective\n"
ROWS = [
    ("rs1", "9", 27_000_000, 0.5, 0.05, 1e-20),
    ("rs2", "9", 27_100_000, 0.1, 0.05, 0.3),
    ("rs3", "6", 30_000_000, 0.2, 0.05, 1e-9),
    ("rs4", "1", 100, 0.01, 0.05, 0.9),
]
REGIONS = {"chr9": ("9", 26_500_000, 28_500_000), "mhc": ("6", 25_000_000, 34_000_000)}


def write_gwas(path, header=HEADER):
    with gzip.open(path, "wt") as handle:
        handle.write(header)
        for rsid, chrom, pos, beta, se, p in ROWS:
            handle.write(f"{rsid}\t{chrom}\t{pos}\ta\tg\t0.3\t{beta}\t{se}\t{p}\t1000\n")


def test_a3_reports_lead_variant_and_counts(tmp_path):
    write_gwas(tmp_path / "g.tsv.gz")
    result = run(tmp_path / "g.tsv.gz", REGIONS)
    assert result["n_variants"] == 4
    assert result["n_genome_wide_significant"] == 2
    chr9 = result["regions"][0]
    assert chr9["variants"] == 2 and chr9["lead"]["rsid"] == "rs1"
    assert result["regions"][1]["lead"]["rsid"] == "rs3"


def test_a3_rejects_schema_drift(tmp_path):
    write_gwas(tmp_path / "g.tsv.gz", header=HEADER.replace("beta", "effect"))
    with pytest.raises(ValueError, match="schema drift"):
        run(tmp_path / "g.tsv.gz", REGIONS)


def test_a3_run_is_verifiable_and_never_overwrites(tmp_path):
    write_gwas(tmp_path / "g.tsv.gz")
    run_gwas_analysis(tmp_path / "g.tsv.gz", tmp_path / "out", run_id="t")
    assert verify_run(tmp_path / "out" / "summary.json")["verified"] is True
    with pytest.raises(FileExistsError):
        run_gwas_analysis(tmp_path / "g.tsv.gz", tmp_path / "out")


def test_a3_missing_input_fails_explicitly(tmp_path):
    with pytest.raises(FileNotFoundError):
        run_gwas_analysis(tmp_path / "nope.tsv.gz", tmp_path / "out")
