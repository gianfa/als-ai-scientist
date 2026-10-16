"""Tests for scripts/data/iedb-cache-acquire.py.

The script's one job is that acquisition and the A4 analysis ask for the same thing: the grid comes
from the paper's own tables through A4's loaders and A4's allele naming, so a pair A4 will look up is
a pair the script requested. Every case is offline — the network is never allowed here — and the
`--offline` mode is itself the check a user runs before trusting a cache.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from test_analysis_a4_hla_binding import cache, tables

from analysis.a4_hla_binding import DQ_ALPHA

REPO_ROOT = Path(__file__).resolve().parents[1]
A, B = "AAAAAAAAAAAAAAA", "BBBBBBBBBBBBBBB"


def _load_module():
    path = REPO_ROOT / "scripts" / "data" / "iedb-cache-acquire.py"
    spec = importlib.util.spec_from_file_location("iedb_cache_acquire", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_grid_is_the_paper_s_peptides_and_its_alleles_named_as_the_api_wants_them(tmp_path):
    """Table 1 repeats a peptide and Table 2 names a DQ beta chain alone: both are A4's problem too."""
    t1, t2 = tables(
        tmp_path,
        [(1, A, 5, 10, 50), (16, B, 2, 10, 20), (31, A, 5, 10, 50)],
        [(A, "DRB1*03:01", 4.9), (B, "DQB1*05:01", 12.0)],
    )
    peptides, alleles = _load_module().grid(t1, t2)
    assert peptides == [A, B]  # de-duplicated, as A4 does
    assert alleles == [f"{DQ_ALPHA}/DQB1*05:01", "HLA-DRB1*03:01"]


def test_offline_refuses_instead_of_acquiring_and_names_what_is_missing(tmp_path, capsys):
    t1, t2 = tables(tmp_path, [(1, A, 5, 10, 50), (16, B, 2, 10, 20)], [(A, "DRB1*03:01", 4.9)])
    partial = cache(tmp_path, [("HLA-DRB1*03:01", A, 4.9)])
    module = _load_module()
    code = module.main(["--table1", str(t1), "--table2", str(t2), "--cache", str(partial), "--offline"])
    assert code == 1
    assert B in capsys.readouterr().err


def test_offline_passes_on_a_covering_cache_and_appends_nothing(tmp_path, capsys):
    t1, t2 = tables(tmp_path, [(1, A, 5, 10, 50)], [(A, "DRB1*03:01", 4.9)])
    full = cache(tmp_path, [("HLA-DRB1*03:01", A, 4.9)])
    before = full.read_text()
    module = _load_module()
    code = module.main(["--table1", str(t1), "--table2", str(t2), "--cache", str(full), "--offline"])
    assert code == 0
    assert full.read_text() == before
    assert "0 new record(s)" in capsys.readouterr().out


def test_a_missing_input_table_stops_the_script(tmp_path, capsys):
    module = _load_module()
    code = module.main(
        [
            "--table1",
            str(tmp_path / "nope.csv"),
            "--table2",
            str(tmp_path / "nope2.csv"),
            "--cache",
            str(tmp_path / "c.jsonl"),
            "--offline",
        ]
    )
    assert code == 2
    assert "docs/data-acquisition.md" in capsys.readouterr().err
