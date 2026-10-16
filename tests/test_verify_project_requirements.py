"""Tests for scripts/dev/verify_project_requirements.py on the tree the public repository receives.

The public repository is this one minus `.release-exclude`, which drops the support directory `.agents`. The verifier must therefore
pass on a clean export of `HEAD` without it (delivery check: "it must pass without git-ignored files").
"""

from __future__ import annotations

import importlib.util
import subprocess
import tarfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_module():
    path = REPO_ROOT / "scripts" / "dev" / "verify_project_requirements.py"
    spec = importlib.util.spec_from_file_location("verify_project_requirements", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _export_head_without_support_directory(dest: Path) -> None:
    archive = dest / "head.tar"
    with archive.open("wb") as out:
        subprocess.run(
            ["git", "-C", str(REPO_ROOT), "archive", "HEAD", "--", ".", ":(exclude).agents"],
            stdout=out,
            check=True,
        )
    with tarfile.open(archive) as tar:
        tar.extractall(dest / "tree", filter="data")


def test_passes_on_a_clean_export_without_the_support_directory(tmp_path: Path) -> None:
    if subprocess.run(["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"], capture_output=True).returncode:
        pytest.skip("not a git checkout")
    _export_head_without_support_directory(tmp_path)
    tree = tmp_path / "tree"
    assert not (tree / ".agents").exists()

    checker = _load_module().RequirementChecker(tree)
    checker.run_all_checks()

    fails = [message for status, message in checker.checks if status == "FAIL"]
    assert fails == []
