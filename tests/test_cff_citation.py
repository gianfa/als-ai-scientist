"""Tests for website/cff_citation.py: the project page's citation is CITATION.cff rendered as BibTeX."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "website" / "cff_citation.py"


def render(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-I", "-B", str(SCRIPT), str(path)], capture_output=True, text=True)


def test_the_repository_citation_renders_its_title_authors_and_dataset_doi() -> None:
    result = render(ROOT / "CITATION.cff")
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("```bibtex\n@software{") and result.stdout.endswith("}\n```\n")
    assert "title   = {{ALS AI Scientist}}" in result.stdout
    assert "author  = {Angelini, Gianfrancesco}" in result.stdout
    assert "@dataset{" in result.stdout and "doi     = {10.5281/zenodo.23370543}" in result.stdout


def test_entities_and_special_characters_are_kept_whole(tmp_path: Path) -> None:
    cff = tmp_path / "CITATION.cff"
    cff.write_text('title: "R&D tool"\nauthors:\n  - name: "Some Lab"\ndate-released: "2025-01-02"\n')
    result = render(cff)
    assert result.returncode == 0, result.stderr
    assert "author = {{Some Lab}}" in result.stdout
    assert "title  = {{R\\&D tool}}" in result.stdout
    assert "year   = {2025}" in result.stdout


def test_a_citation_without_a_year_fails_loudly(tmp_path: Path) -> None:
    cff = tmp_path / "CITATION.cff"
    cff.write_text('title: "Tool"\nauthors:\n  - family-names: Doe\n')
    result = render(cff)
    assert result.returncode == 1 and "has no year" in result.stderr
