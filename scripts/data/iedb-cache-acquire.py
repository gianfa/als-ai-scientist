#!/usr/bin/env python3
"""Acquire the IEDB/NetMHCIIpan prediction cache that the A4 analysis reads.

A4 (`analysis/a4_hla_binding.py`) is deterministic and offline: it reads the append-only cache at
`data/external/iedb-cache/netmhciipan.jsonl` and never calls the service. This script is
the one step that does call it. The grid is not chosen here: the peptides are Table 1 of Michaelis
et al. and the alleles are those of its Table 2, read through the same loaders and the same allele
naming the analysis uses, so acquisition and analysis cannot disagree about what was asked for.

    scripts/data/iedb-cache-acquire.py --offline   # check an existing cache covers the grid; no network
    scripts/data/iedb-cache-acquire.py             # request what is missing (one request per peptide)

Licence (LICENSES.md): the NetMHC component is served under the DTU academic licence,
free for a non-profit academic or research institution only; a non-academic user must obtain a
commercial licence, and benchmark results of the predictor may not be published. The cache is
git-ignored and never redistributed.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))  # run as a script from scripts/: make the first-party packages importable

from analysis.a4_hla_binding import LENGTH, METHOD, allele_name
from analysis.datasets import load_table1, load_table2
from tools.data.netmhciipan import DEFAULT_CACHE, CacheMiss, predict

PAPER = Path("dist/michaelis-2025-agent/skill/michaelis-2025-paper/assets/table")
DEFAULT_TABLE1 = PAPER / "table-p0006-001.csv"
DEFAULT_TABLE2 = PAPER / "table-p0006-007.csv"


def grid(table1: Path, table2: Path) -> tuple[list[str], list[str]]:
    """The peptides and the API allele names the A4 analysis will look for in the cache."""
    peptides = sorted({epitope.sequence for epitope in load_table1(table1)})
    rows = load_table2(table2)
    allele_field = list(rows[0])[1]
    alleles = sorted({allele_name(str(row[allele_field])) for row in rows})
    return peptides, alleles


def cached_records(cache: Path) -> int:
    return sum(1 for line in cache.read_text().splitlines() if line.strip()) if cache.is_file() else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--table1", type=Path, default=DEFAULT_TABLE1, help="Michaelis Table 1 CSV (epitopes)")
    parser.add_argument("--table2", type=Path, default=DEFAULT_TABLE2, help="Michaelis Table 2 CSV (HLA)")
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE, help=f"cache file (default {DEFAULT_CACHE})")
    parser.add_argument(
        "--offline", action="store_true", help="forbid the network: report whether the cache already covers the grid"
    )
    args = parser.parse_args(argv)

    for path in (args.table1, args.table2):
        if not path.is_file():
            print(f"error: missing input {path} (see docs/data-acquisition.md)", file=sys.stderr)
            return 2

    peptides, alleles = grid(args.table1, args.table2)
    before = cached_records(args.cache)
    print(f"grid: {len(peptides)} peptides x {len(alleles)} alleles = {len(peptides) * len(alleles)} pairs")
    print(f"alleles: {', '.join(alleles)}")
    print(f"cache: {args.cache} ({before} records before)")

    try:
        predictions = predict(
            peptides, alleles, length=LENGTH, method=METHOD, cache_path=args.cache, allow_network=not args.offline
        )
    except CacheMiss as exc:
        print(f"not covered by the cache, and --offline forbids the network: {exc}", file=sys.stderr)
        return 1

    after = cached_records(args.cache)
    print(f"resolved {len(predictions)} pairs; {after - before} new record(s) appended")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
