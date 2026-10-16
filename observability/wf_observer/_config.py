"""Configuration for wf_observer observability system."""

from pathlib import Path

# Project root: resolve from this file's location
# _config.py is at: observability/wf_observer/_config.py, so the root is 3 levels up.
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Results and experiments directories
EXPERIMENTS_ROOT = PROJECT_ROOT / "experiments"
SMOKE_ROOT = PROJECT_ROOT / "results" / "smoke"
ARTIFACTS_ROOT = PROJECT_ROOT / "artifacts"

# Default run storage
DEFAULT_RUN_STORE = PROJECT_ROOT / "results" / "runs"
