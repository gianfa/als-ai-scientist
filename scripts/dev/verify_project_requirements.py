#!/usr/bin/env python3
"""
Deterministic project requirements verifier.

Checks structural and presence-based requirements before workflow launch.
Does NOT evaluate scientific quality.

Exit code: 0 = all checks PASS/WARN, 1 = at least one FAIL
"""

import sys
from pathlib import Path
from typing import List, Tuple


def find_project_root() -> Path:
    """Find the project root: the nearest directory that holds `run.sh` and `pipeline/`."""
    current = Path.cwd()
    for parent in [current] + list(current.parents):
        if (parent / "run.sh").is_file() and (parent / "pipeline").is_dir():
            return parent

    # Fallback: assume we're in or near the project root
    if (current / ".agents").exists() or (current / "pipeline").exists():
        return current

    raise RuntimeError("Cannot find project root. Are we in the right directory?")


class RequirementChecker:
    def __init__(self, project_root: Path):
        self.root = project_root
        self.checks: List[Tuple[str, str]] = []

    def add_check(self, status: str, message: str):
        """Record a check result. Status: PASS, WARN, or FAIL."""
        self.checks.append((status, message))

    def check_dir_exists(self, rel_path: str, name: str) -> bool:
        """Check if a directory exists."""
        path = self.root / rel_path
        exists = path.is_dir()
        self.add_check("PASS" if exists else "FAIL", f"{name}")
        return exists

    def check_file_exists(self, rel_path: str, name: str) -> bool:
        """Check if a file exists."""
        path = self.root / rel_path
        exists = path.is_file()
        self.add_check("PASS" if exists else "FAIL", f"{name}")
        return exists

    def check_content(self, rel_path: str, substring: str, name: str) -> bool:
        """Check if a file contains a specific substring."""
        path = self.root / rel_path
        if not path.is_file():
            self.add_check("FAIL", f"{name} - file not found")
            return False
        try:
            content = path.read_text()
            found = substring in content
            self.add_check("PASS" if found else "FAIL", f"{name}")
            return found
        except Exception:
            self.add_check("FAIL", f"{name} - error reading file")
            return False

    def check_optional_file(self, rel_path: str, name: str) -> bool:
        """Check if an optional file exists (warns if missing)."""
        path = self.root / rel_path
        exists = path.is_file()
        self.add_check("PASS" if exists else "WARN", f"{name} (optional)")
        return exists

    def check_dir_populated(self, rel_path: str, name: str) -> bool:
        """Check if a directory has at least one file."""
        path = self.root / rel_path
        if not path.is_dir():
            self.add_check("FAIL", f"{name} - directory not found")
            return False
        has_content = len(list(path.glob("*"))) > 0
        self.add_check("PASS" if has_content else "WARN", f"{name} populated")
        return has_content

    def check_michaelis_data(self) -> None:
        """Warn (not fail) if the user-provided Michaelis ICS data are missing."""
        data = self.root / "data/external/michaelis-2025-zenodo"
        n = len(list(data.rglob("*.fcs"))) if data.is_dir() else 0
        self.add_check(
            "PASS" if n else "WARN",
            f"Michaelis Zenodo FCS data present ({n} .fcs files; user-provided, optional)",
        )

    def run_all_checks(self):
        """Run all project requirement checks."""

        # 1. Required directories: the ones a launch actually reads.
        self.check_dir_exists("pipeline", "Pipeline package")
        self.check_dir_exists("tools", "Tool packages")
        self.check_dir_exists("agents", "Agents directory")
        self.check_dir_exists("configs", "Configs directory")
        self.check_dir_exists("scripts", "Scripts directory")
        self.check_dir_exists("tests", "Tests directory")

        # 2. Root documentation and configuration
        self.check_file_exists("README.md", "Root README")
        self.check_optional_file("run.sh", "Run script")

        # 3. Minimum required agents (documented in docs/agents.md)
        self.check_content("docs/agents.md", "Evidence Agent", "Evidence agent role documented")
        self.check_content("docs/agents.md", "Hypothesis Generator", "Hypothesis generator role documented")
        self.check_content("docs/agents.md", "Adversarial Critic", "Adversarial critic role documented")

        # 4. Main workflow and agents
        self.check_dir_populated("agents", "Agents directory")

        # 5. Evaluation module
        self.check_dir_exists("evaluation", "Evaluation module")
        self.check_dir_populated("evaluation", "Evaluation code")

        # 6. E2E artifacts directory
        self.check_dir_exists("artifacts", "E2E artifacts directory")

        # 8. Configuration files
        self.check_dir_exists("configs", "Configs directory")

        # 10a. User-provided data acquisition (data themselves are local-only)
        self.check_file_exists("scripts/data/michaelis-data-download.sh", "Michaelis data download script")
        self.check_content(
            "docs/data-acquisition.md",
            "michaelis-data-download.sh",
            "Data acquisition documented",
        )
        self.check_michaelis_data()

        # 10. Intermediate results directory
        self.check_dir_exists("results", "Results directory")

    def report(self) -> int:
        """Print report and return exit code."""
        if not self.checks:
            print("[FAIL] No checks were run")
            return 1

        for status, message in self.checks:
            print(f"[{status}] {message}")

        has_fail = any(status == "FAIL" for status, _ in self.checks)

        # Summary
        passes = sum(1 for s, _ in self.checks if s == "PASS")
        warns = sum(1 for s, _ in self.checks if s == "WARN")
        fails = sum(1 for s, _ in self.checks if s == "FAIL")

        print(f"\nSummary: {passes} passed, {warns} warned, {fails} failed")

        return 1 if has_fail else 0


def main():
    try:
        project_root = find_project_root()
        print(f"Project root: {project_root}\n")
    except RuntimeError as e:
        print(f"[FAIL] {e}", file=sys.stderr)
        return 1

    checker = RequirementChecker(project_root)
    checker.run_all_checks()
    return checker.report()


if __name__ == "__main__":
    sys.exit(main())
