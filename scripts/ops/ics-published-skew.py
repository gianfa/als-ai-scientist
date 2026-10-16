#!/usr/bin/env python3
"""Run the `ics_cytokine_skew_v1` test on the published source data instead of the recomputed values.

The handler recomputes each donor's CD4+ cytokine responses from the deposited FlowJo workspaces and tests the
IFNg / (IL-4 + IL-10) ratio, ALS against healthy donors. Only 7 of its 30 donors equal a published row, so the
question is whether the test result depends on that disagreement. This script applies the same ratio, floor and
exact Mann-Whitney test to the 15 + 15 published rows (sheet "ED Fig. 4", panel e) and prints both results.

Not part of the pipeline: an inspection utility for the technical report.

    scripts/ops/ics-published-skew.py
    scripts/ops/ics-published-skew.py --source-data path/to/41586_2025_9588_MOESM3_ESM.xlsx
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from statistics import median
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from analysis.experiment_ics_cytokines import CYTOKINES, FLOOR, mann_whitney_exact, read_source_data

DEFAULT_SOURCE_DATA = REPO_ROOT / "data/external/michaelis-2025-nature-supp/41586_2025_9588_MOESM3_ESM.xlsx"


def skew_ratio(row: tuple[float, ...]) -> float:
    """IFNg / (IL-4 + IL-10) of one published row, each response floored as the handler floors it."""
    value = {name: max(v, FLOOR) for name, v in zip(CYTOKINES, row)}
    return value["IFNg"] / (value["IL-4"] + value["IL-10"])


def published_skew(published: dict[str, list[tuple[float, ...]]]) -> dict[str, Any]:
    """The handler's test, applied to the published rows of both groups."""
    als = [skew_ratio(row) for row in published["ALS"]]
    hc = [skew_ratio(row) for row in published["HC"]]
    u, p = mann_whitney_exact(als, hc)
    return {
        "n": {"ALS": len(als), "HC": len(hc)},
        "median_skew_ratio": {"ALS": median(als), "HC": median(hc)},
        "mann_whitney_u": u,
        "p_value": p,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source-data", type=Path, default=DEFAULT_SOURCE_DATA, help="the article's source data")
    args = parser.parse_args()
    if not args.source_data.exists():
        print(f"source data not found: {args.source_data} (docs/data-acquisition.md, section 3)", file=sys.stderr)
        return 1
    print(json.dumps(published_skew(read_source_data(args.source_data)), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
