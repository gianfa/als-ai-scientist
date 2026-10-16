"""Tests for scripts/setup/check-paper2agent.sh (repository root).

The script verifies the paper extraction a run reads, not only that the Paper2Agent skill is installed:
an installed skill can still produce tables whose names or headers the `data` phase does not expect. These
tests pin two properties: the expected files come from the configuration, not from a list repeated in the
script, and a missing one fails the check.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "setup" / "check-paper2agent.sh"
EXTRACTION = "dist/michaelis-2025-agent/skill/michaelis-2025-paper"


def check(root: Path, config: str = "configs/c.yaml") -> subprocess.CompletedProcess[str]:
    """Run the script against a tree we control, with no Paper2Agent skill in HOME."""
    return subprocess.run(
        ["bash", str(SCRIPT), config],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "PAPER2AGENT_ROOT": str(root), "HOME": str(root / "home")},
    )


def tree(root: Path, *, tables: tuple[str, ...], paper_md: bool = True) -> None:
    """A configuration naming two tables, and an extraction that may or may not satisfy it."""
    (root / "configs").mkdir(parents=True, exist_ok=True)
    (root / "configs" / "c.yaml").write_text(
        "phases:\n  data:\n    sources:\n"
        f"    - path: {EXTRACTION}/assets/table/table-p0006-001.csv\n"
        f"    - path: {EXTRACTION}/assets/table/table-p0006-007.csv\n"
    )
    assets = root / EXTRACTION / "assets" / "table"
    assets.mkdir(parents=True, exist_ok=True)
    for name in tables:
        (assets / name).write_text("Peptide residue,Peptide sequence\n")
    if paper_md:
        references = root / EXTRACTION / "references"
        references.mkdir(parents=True, exist_ok=True)
        (references / "paper.md").write_text("# Michaelis et al. 2025\n")


def test_a_complete_extraction_passes(tmp_path: Path) -> None:
    tree(tmp_path, tables=("table-p0006-001.csv", "table-p0006-007.csv"))
    result = check(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "the extraction configs/c.yaml expects is present" in result.stdout


def test_a_renamed_table_fails_and_says_why(tmp_path: Path) -> None:
    """The extraction exists, but the names are not the ones the config addresses."""
    tree(tmp_path, tables=("table1.csv", "table2.csv"))
    result = check(tmp_path)
    assert result.returncode == 1
    assert result.stdout.count("FAIL") == 2
    assert "keep Paper2Agent's default asset names" in result.stdout


def test_a_missing_paper_markdown_fails(tmp_path: Path) -> None:
    tree(tmp_path, tables=("table-p0006-001.csv", "table-p0006-007.csv"), paper_md=False)
    result = check(tmp_path)
    assert result.returncode == 1
    assert "references/paper.md is missing" in result.stdout


def test_an_empty_table_is_not_a_present_one(tmp_path: Path) -> None:
    tree(tmp_path, tables=("table-p0006-001.csv", "table-p0006-007.csv"))
    (tmp_path / EXTRACTION / "assets" / "table" / "table-p0006-007.csv").write_text("")
    result = check(tmp_path)
    assert result.returncode == 1
    assert "is empty" in result.stdout


def test_a_missing_skill_alone_does_not_fail_the_check(tmp_path: Path) -> None:
    """The skill builds the extraction; it is not needed to read one that already exists."""
    tree(tmp_path, tables=("table-p0006-001.csv", "table-p0006-007.csv"))
    result = check(tmp_path)
    assert result.returncode == 0
    assert "note  not installed" in result.stdout


def test_a_configuration_that_declares_no_table_fails(tmp_path: Path) -> None:
    (tmp_path / "configs").mkdir(parents=True)
    (tmp_path / "configs" / "c.yaml").write_text("phases:\n  data:\n    sources: []\n")
    result = check(tmp_path)
    assert result.returncode == 1
    assert "declares no table" in result.stdout


def test_a_missing_configuration_fails_instead_of_passing_vacuously(tmp_path: Path) -> None:
    result = check(tmp_path, config="configs/absent.yaml")
    assert result.returncode == 1
    assert "no such configuration" in result.stdout


def test_the_repository_s_own_configuration_is_satisfied_by_the_checkout() -> None:
    """Skipped where the extraction is absent, which is every fresh clone: it is user-provided input."""
    if not (REPO_ROOT / EXTRACTION).is_dir():
        pytest.skip("local Paper2Agent extraction not present (docs/data-acquisition.md section 1.1)")
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, result.stdout + result.stderr
