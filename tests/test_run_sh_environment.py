"""`run.sh` resolves the virtualenv of *this* checkout, whatever the shell exported.

Entry 9 of the bug observatory: the script held `export UV_PROJECT_ENVIRONMENT="${UV_PROJECT_ENVIRONMENT:-.venv}"`,
which substitutes only when the variable is unset or empty — and this devcontainer *exports*
`UV_PROJECT_ENVIRONMENT=/opt/cdqn-venv`, another project's environment. The default therefore never applied, and
a launch synced this project's dependencies into that venv: 36 packages, then 24 installed and 2
uninstalled. The line read as a guard and was inert in the only situation it existed for.

So these tests run the script's resolution with the variable set the way the container sets it. They do not
launch anything: `run.sh` ends in `exec uv run python -m pipeline.cli "$@"`, and a stub `uv` on `PATH` prints
what it was given and exits, so the decision is observed without a paid run and without touching any venv.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
RUN_SH = REPO / "run.sh"


@pytest.fixture
def stub_path(tmp_path: Path) -> str:
    """A `PATH` whose `uv` and `claude` only report, so `run.sh` can be executed to its last line."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("uv", "claude"):
        stub = bin_dir / name
        stub.write_text(f'#!/usr/bin/env bash\necho "{name}: $*"\necho "env=$UV_PROJECT_ENVIRONMENT"\n')
        stub.chmod(0o755)
    return f"{bin_dir}:{os.environ.get('PATH', '')}"


def resolved(stub_path: str, value: str | None) -> str:
    """What `run.sh` exports as `UV_PROJECT_ENVIRONMENT` when the shell held `value`."""
    env = {"PATH": stub_path, "HOME": os.environ.get("HOME", "/tmp")}
    if value is not None:
        env["UV_PROJECT_ENVIRONMENT"] = value
    done = subprocess.run(
        ["bash", str(RUN_SH), "--run-id", "test-only"], capture_output=True, text=True, cwd=REPO, env=env, timeout=60
    )
    assert done.returncode == 0, done.stderr
    [line] = [ln for ln in done.stdout.splitlines() if ln.startswith("env=")]
    return line.removeprefix("env=")


def test_an_exported_foreign_virtualenv_is_overridden_and_the_override_is_announced(stub_path):
    """The case the guard exists for, and the one the old `${VAR:-default}` could not catch."""
    env = {"PATH": stub_path, "HOME": os.environ.get("HOME", "/tmp"), "UV_PROJECT_ENVIRONMENT": "/opt/cdqn-venv"}
    done = subprocess.run(
        ["bash", str(RUN_SH), "--run-id", "test-only"], capture_output=True, text=True, cwd=REPO, env=env, timeout=60
    )
    assert done.returncode == 0, done.stderr
    assert f"env={REPO}/.venv" in done.stdout
    # Announced, not silent: using another project's environment without saying so is what went wrong.
    assert "ignoring UV_PROJECT_ENVIRONMENT=/opt/cdqn-venv" in done.stderr


@pytest.mark.parametrize("exported", [None, "", ".venv", "./.venv", "relative/elsewhere"])
def test_unset_empty_and_relative_all_become_this_checkout_s_venv(stub_path, exported):
    assert resolved(stub_path, exported) == f"{REPO}/.venv"


def test_an_absolute_path_inside_the_checkout_is_honoured(stub_path):
    """An explicit choice is a choice: only a path leaving the checkout is overridden."""
    chosen = f"{REPO}/.venv-experiment"
    assert resolved(stub_path, chosen) == chosen


def test_the_script_still_refuses_to_launch_without_a_run_id(stub_path):
    """The guard must not have become a way past the paid-run check."""
    done = subprocess.run(
        ["bash", str(RUN_SH)],
        capture_output=True,
        text=True,
        cwd=REPO,
        env={"PATH": stub_path, "HOME": os.environ.get("HOME", "/tmp")},
        timeout=60,
    )
    assert done.returncode == 2 and "--run-id is required" in done.stderr
