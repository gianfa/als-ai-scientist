"""`scripts/ops/ics-published-skew.py`: the handler's skew test applied to published rows."""

from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/ops/ics-published-skew.py"


def _module():
    spec = importlib.util.spec_from_file_location("ics_published_skew", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_skew_ratio_floors_each_response():
    # Rows are ordered as CYTOKINES: IFNg, IL-4, IL-10, IL-17.
    assert _module().skew_ratio((0.0, 0.0, 0.0, 5.0)) == 0.01 / 0.02


def test_published_skew_separates_groups():
    published = {
        "ALS": [(0.1, 1.0, 1.0, 0.0), (0.2, 1.0, 1.0, 0.0), (0.1, 2.0, 0.0, 0.0)],
        "HC": [(2.0, 0.1, 0.1, 0.0), (3.0, 0.1, 0.1, 0.0), (4.0, 0.2, 0.0, 0.0)],
    }
    result = _module().published_skew(published)
    assert result["n"] == {"ALS": 3, "HC": 3}
    assert result["median_skew_ratio"]["ALS"] < result["median_skew_ratio"]["HC"]
    assert result["mann_whitney_u"] == 0.0
    assert result["p_value"] == 0.1  # exact two-sided, 3 vs 3, complete separation: 2 / C(6, 3)
