"""Tests for scripts/setup/prepare-environment.sh: the dry run changes nothing and narrates, a bad argument is refused."""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "setup" / "prepare-environment.sh"


def setup(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, timeout=60)


def test_dry_run_narrates_every_step_and_executes_nothing() -> None:
    result = setup("--dry-run", "--skip-data")
    assert result.returncode == 0, result.stderr
    for step in range(1, 7):
        assert f"step {step}/6" in result.stdout
    assert "[dry-run] uv sync --frozen" in result.stdout
    assert "npm ci" not in result.stdout


def test_unknown_argument_is_refused() -> None:
    result = setup("--nope")
    assert result.returncode == 1
    assert "[fail] unknown argument '--nope'" in result.stderr
