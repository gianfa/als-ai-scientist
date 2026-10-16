#!/usr/bin/env python3
"""One-off generator of this fixture's synthetic ICS deposit and source-data spreadsheet.

Not a test, not collected by pytest. Run it manually only when the fixture's shape must change; its output is
deterministic (`tests/ics_fixtures.py` writes no timestamp or random content), so re-running it regenerates the
same bytes it already committed, and that is how to check the committed files are still what this script writes.

    UV_PROJECT_ENVIRONMENT=.venv uv run --frozen python tests/fixtures/execute/build_ics_deposit.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))  # repo root, for `analysis`
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # tests/, for `ics_fixtures`

from ics_fixtures import DONORS, deposit, expected_rows, source_data  # noqa: E402


def main() -> None:
    fixture_root = Path(__file__).resolve().parent
    data_root = fixture_root / "data" / "ics_cytokine_skew_v1"
    if data_root.exists():
        shutil.rmtree(data_root)
    data_root.mkdir(parents=True)
    deposit(data_root, DONORS)
    # Every donor of the deposit reproduces this fixture's own "published" source data, mirroring the real
    # analysis's reproduction check against a small, fully-controlled cohort.
    source_data(data_root / "source.xlsx", {g: expected_rows(g) for g in DONORS})
    print(f"wrote {data_root}")


if __name__ == "__main__":
    main()
