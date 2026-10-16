"""Tests for scripts/ops/check-running-workflows.sh (repository root).

This gate decides whether a new run may start. Two failure modes are covered: a crashed workflow
leaves its journal ending on `started` and must not block every later run, and `((count++))` under
`set -e` must not abort the script before it names what it found.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "ops" / "check-running-workflows.sh"


def workflow(base: Path, workflow_id: str, last_event: dict, idle_minutes: int = 0) -> Path:
    """One workflow journal under a fake workflows base, aged by idle_minutes."""
    directory = base / f"session-{workflow_id}" / "subagents" / "workflows" / workflow_id
    directory.mkdir(parents=True)
    journal = directory / "journal.jsonl"
    journal.write_text(json.dumps({"type": "launched"}) + "\n" + json.dumps(last_event) + "\n")
    if idle_minutes:
        old = time.time() - idle_minutes * 60
        os.utime(journal, (old, old))
    return journal


def check(base: Path, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "WORKFLOWS_BASE": str(base), **env},
    )


def test_a_fresh_started_workflow_blocks_a_new_run(tmp_path):
    workflow(tmp_path, "wf_live", {"type": "started", "label": "evidence-1"}, idle_minutes=0)

    completed = check(tmp_path)

    assert completed.returncode == 1
    assert "wf_live" in completed.stdout
    # The list must actually reach the caller: a bare exit 1 is what made the earlier bug opaque.
    assert "wf_live" in completed.stderr


def test_a_crashed_workflow_does_not_block_new_runs_forever(tmp_path):
    """A crashed workflow's journal can end on `started` hours after the process died."""
    workflow(tmp_path, "wf_crashed", {"type": "started", "label": "evidence-checker"}, idle_minutes=244)

    completed = check(tmp_path)

    assert completed.returncode == 0
    assert "stale" in completed.stderr
    assert "wf_crashed" in completed.stderr


@pytest.mark.parametrize("event", [{"type": "result", "result": "x"}, {"type": "failed"}, {"type": "launched"}])
def test_terminal_journals_never_block(tmp_path, event):
    workflow(tmp_path, "wf_done", event)

    assert check(tmp_path).returncode == 0


def test_an_empty_base_is_not_a_blocker(tmp_path):
    assert check(tmp_path).returncode == 0
    assert check(tmp_path / "missing").returncode == 0


def test_the_staleness_threshold_is_configurable(tmp_path):
    workflow(tmp_path, "wf_slow", {"type": "started"}, idle_minutes=20)

    assert check(tmp_path, STALE_MINUTES="15").returncode == 0
    assert check(tmp_path, STALE_MINUTES="60").returncode == 1
