#!/usr/bin/env python3
"""Manual check of `tools/data/pharmacology.py` against the real MyChem.info service.

Not part of pytest (the default suite never touches the network). No acquisition script exists for this cache,
because the names come from `intervene`, run by run. This one only shows what the live service says
for the names given, with the matching rules of the client.

    scripts/ops/pharmacology-smoke.py metformin imatinib rapamycin
    scripts/ops/pharmacology-smoke.py --cache /path/to/scratch.jsonl metformin
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from tools.data.pharmacology import DEFAULT_CACHE, lookup_match


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("names", nargs="+")
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE, help="JSONL cache (default: the run cache)")
    args = parser.parse_args()
    for name in args.names:
        match, record, url = lookup_match(name, cache_path=args.cache)
        detail = (
            (
                f"drugbank={record.drugbank_id} chembl={record.chembl_id} chebi={record.chebi_id} "
                f"pubchem={record.pubchem_cid} unii={record.unii}"
            )
            if record
            else "-"
        )
        print(f"{name}: {match}  {detail}\n  {url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
