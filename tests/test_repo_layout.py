"""The repository layout, and the paths that must not quietly depend on it.

The implementation once lived in an `als-ai-scientist/` package directory and now lives at the repository
root. A path that is correct relative to itself can be wrong relative to where the process runs, and unit
tests that run from the repository root do not notice. These tests check the properties that matter: roots
are absolute, resolve inside the checkout, and do not depend on the working directory.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# The directory the implementation used to live in. Historical records keep their old paths on purpose,
# so they are not scanned.
REMOVED_PACKAGE_DIR = "als-ai-scientist"
HISTORICAL = (
    ".agents",
    ".meta",
    "reports",
    "uv.lock",
    "package-lock.json",
    "node_modules",
    ".venv",
    ".git",
)

# What counts as an offender is a **path**, not every mention of the string: the repository's own directory is
# named after the project, a User-Agent header carries the name, and prose that explains the move must be able
# to name what moved. So the scan looks for the two shapes that break at run time — a path segment, and a `cd`
# into the directory — and nothing else.
# The public repository shares the removed directory's name: a GitHub link to it is not a path into the checkout,
# and neither is the public container's workspace, /app/als-ai-scientist (publish-public.sh step 2b2).
# A `cd` into the removed directory is a stale path in the private source, whose directory is named otherwise, but the
# right one in the public repository, where publish-public.sh (step 2b) rewrites the clone instruction to it. The
# public tree is the one without `.release-exclude`, which the export always drops.
_PATH_SEGMENT = r"(?<!project10-)(?<!github\.com/gianfa/)(?<!/app/)als-ai-scientist/[A-Za-z0-9_.]"
_CD_INTO_DIR = r"\bcd +als-ai-scientist\b"
IS_PRIVATE_SOURCE = (REPO_ROOT / ".release-exclude").exists()
PATH_REFERENCE = re.compile(f"{_PATH_SEGMENT}|{_CD_INTO_DIR}" if IS_PRIVATE_SOURCE else _PATH_SEGMENT)


def _tracked_files() -> list[Path]:
    import subprocess

    out = subprocess.run(["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True, check=True)
    return [REPO_ROOT / line for line in out.stdout.split("\n") if line]


def _is_historical(path: Path) -> bool:
    rel = path.relative_to(REPO_ROOT).as_posix()
    return any(rel == h or rel.startswith(h + "/") for h in HISTORICAL)


# --- the required tree ---------------------------------------------------------------------------

#: The repository layout. `model/`, `training/` and `notebooks/` are deliberately absent: no model is trained
#: and no notebook exists, and empty directories would imply work that was never done.
REQUIRED_DIRS = (
    "agents",
    "data",
    "datasets",
    "literature",
    "evaluation",
    "experiments",
    "configs",
    "results",
    "reports",
)
DECLARED_ABSENT = ("model", "training", "notebooks")
# The three directories of a training project that this one deliberately does not have. Checking their absence is
# skipped by default; `ALS_LAYOUT_SKIP_ABSENT_DIRS=0` runs the check.
SKIP_ABSENT_DIRS = os.environ.get("ALS_LAYOUT_SKIP_ABSENT_DIRS", "1") != "0"
REQUIRED_FILES = ("README.md", "requirements.txt", "environment.yml", "run.sh")


@pytest.mark.parametrize("name", REQUIRED_DIRS)
def test_required_directories_are_at_the_repository_root(name: str) -> None:
    assert (REPO_ROOT / name).is_dir(), f"{name}/ is required by the repository layout"


@pytest.mark.parametrize("name", REQUIRED_FILES)
def test_required_root_files_exist(name: str) -> None:
    assert (REPO_ROOT / name).is_file()


@pytest.mark.skipif(SKIP_ABSENT_DIRS, reason="skipped by default; set ALS_LAYOUT_SKIP_ABSENT_DIRS=0 to check")
@pytest.mark.parametrize("name", DECLARED_ABSENT)
def test_directories_without_content_are_absent(name: str) -> None:
    """An empty directory would imply work that does not exist."""
    assert not (
        REPO_ROOT / name
    ).exists(), (
        f"{name}/ exists: either it now holds real work, or it is an empty directory implying work that does not exist"
    )


def test_the_removed_package_directory_is_gone() -> None:
    assert not (REPO_ROOT / REMOVED_PACKAGE_DIR).exists()


def test_no_tracked_file_still_points_into_the_removed_package_directory() -> None:
    """A path into the removed directory resolves outside the checkout and fails only at run time."""
    offenders: list[str] = []
    for path in _tracked_files():
        if _is_historical(path) or not path.is_file():
            continue
        try:
            text = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if not PATH_REFERENCE.search(line):
                continue
            offenders.append(f"{path.relative_to(REPO_ROOT)}:{number}: {line.strip()[:100]}")
    assert not offenders, "references to the removed package directory:\n" + "\n".join(offenders)


# --- paths that must not depend on the working directory -----------------------------------------


def test_observer_roots_are_absolute_and_inside_the_repository() -> None:
    from observability.wf_observer import _config

    for name in ("PROJECT_ROOT", "EXPERIMENTS_ROOT", "SMOKE_ROOT", "ARTIFACTS_ROOT", "DEFAULT_RUN_STORE"):
        value: Path = getattr(_config, name)
        assert value.is_absolute(), f"{name} is relative: it would resolve against the caller's cwd"
        assert value == REPO_ROOT or REPO_ROOT in value.parents, f"{name} points outside the repository: {value}"


def test_observer_cli_path_defaults_are_absolute() -> None:
    """`--e2e-root` defaulted to `../experiments`, which was only ever right from inside the package."""
    from observability.wf_observer.cli import _parser

    defaults = {action.dest: action.default for action in _parser()._actions if isinstance(action.default, Path)}
    assert defaults, "no path default found: has the parser changed?"
    relative = {name: value for name, value in defaults.items() if not value.is_absolute()}
    assert not relative, f"path defaults that resolve against the cwd: {relative}"


def test_observer_finds_the_experiments_from_any_working_directory(tmp_path: Path, monkeypatch) -> None:
    """Build the defaults from another directory: a relative default is only wrong from elsewhere."""
    from observability.wf_observer.cli import _parser

    monkeypatch.chdir(tmp_path)
    args = _parser().parse_args(["serve"])
    assert args.e2e_root.is_absolute() and args.e2e_root == REPO_ROOT / "experiments"
    assert args.smoke_root.is_absolute()
    assert args.root.is_absolute()
    assert os.getcwd() == str(tmp_path)  # the defaults did not come from the cwd


def test_the_catalog_s_default_root_is_the_repository_s_experiments(tmp_path: Path, monkeypatch) -> None:
    """End of the same chain, first half: the default root does not come from the working directory."""
    from observability.wf_observer._config import EXPERIMENTS_ROOT

    monkeypatch.chdir(tmp_path)
    assert EXPERIMENTS_ROOT.is_absolute()
    assert EXPERIMENTS_ROOT == REPO_ROOT / "experiments"


def test_the_catalog_lists_an_archive_from_another_working_directory(tmp_path: Path, monkeypatch) -> None:
    """Second half: given an absolute root, the catalog finds its runs wherever it is called from.

    The archive is built here rather than read from `experiments/`, which is git-ignored and empty in a
    fresh clone.
    """
    from observability.wf_observer.catalog import RunCatalog

    archive = tmp_path / "archive"
    results = archive / "pl-test-001" / "results"
    results.mkdir(parents=True)
    (results / "context.json").write_text(json.dumps({"started_utc": "2026-10-08T07:00:00Z"}))
    (archive / "scaffolding-only").mkdir()

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    runs = RunCatalog(experiments_root=archive, smoke_root=tmp_path / "smoke").list()

    assert [ref.id for ref in runs] == [
        "pl-test-001"
    ], "expected exactly the one run with a results/ directory; an empty directory is scaffolding"


def test_the_literature_store_resolves_the_repository_root(tmp_path: Path, monkeypatch) -> None:
    """`tools/literature/store.py` walks up the directory tree to find the root."""
    from tools.literature.store import REPO_ROOT as STORE_REPO_ROOT

    monkeypatch.chdir(tmp_path)
    assert STORE_REPO_ROOT == REPO_ROOT
    assert (STORE_REPO_ROOT / "literature" / "reading-lists").is_dir()


@pytest.mark.parametrize("config", sorted((REPO_ROOT / "configs").glob("pipeline*.yaml")), ids=lambda path: path.name)
def test_the_configured_literature_store_is_inside_the_checkout(config: Path) -> None:
    """The test above checks the module's root; this one checks the **configured value**, which is what a run
    actually opens. A relative path that escapes the checkout silently creates a second, near-empty store.

    Parametrised over every pipeline config: a fix applied to the config this test loads can leave the others
    with the broken path, and a guard that reads only the default is not a guard on the configs.
    """
    from pipeline.config import load_config

    store = load_config(config).store_path()
    assert str(store).startswith(str(REPO_ROOT)), f"{config.name}: the store resolves outside the checkout: {store}"
    assert store.parent == REPO_ROOT / "literature", f"{config.name}: the store is not under literature/"


def test_the_analysis_layer_resolves_the_repository_root() -> None:
    """`analysis/` stamps the git commit of the repository into every summary it seals."""
    from analysis import runner

    stamped = Path(runner.__file__).resolve().parents[1]
    assert stamped == REPO_ROOT
    assert (stamped / ".git").exists(), "the analysis layer would stamp a commit from outside the repository"


# --- the observer must read the archives the current architecture writes -------------------------


def test_the_observer_finds_the_phase_pipeline_s_journals() -> None:
    """The phase pipeline writes one journal per phase as `NN-<phase>-journal.jsonl`, not the single
    `discovery-journal.jsonl` / `journal.jsonl` of the older launcher; `/runs/<id>/events` must find them.
    """
    from observability.wf_observer.server import _workflow_journals

    archives = sorted((REPO_ROOT / "experiments").glob("pl-prod-*/results"))
    if not archives:
        pytest.skip("no archived production run in this checkout")
    results_dir = archives[-1]
    journals = _workflow_journals(results_dir)
    assert journals, f"no journal found in {results_dir}"
    assert all(path.is_file() for path in journals)
    # Phase order is the `NN` prefix, so the listing must be sorted and not an arbitrary glob order.
    assert [p.name for p in journals] == sorted(p.name for p in journals)


def test_a_legacy_single_journal_still_wins_over_the_per_phase_glob(tmp_path: Path) -> None:
    """The old archives must keep working: one journal named the old way is returned alone."""
    from observability.wf_observer.server import _workflow_journals

    (tmp_path / "discovery-journal.jsonl").write_text('{"type":"launched"}\n')
    (tmp_path / "03-literature-journal.jsonl").write_text('{"type":"launched"}\n')
    assert [p.name for p in _workflow_journals(tmp_path)] == ["discovery-journal.jsonl"]


def test_a_results_directory_with_no_journal_yields_none(tmp_path: Path) -> None:
    from observability.wf_observer.server import _workflow_journals

    assert _workflow_journals(tmp_path) == []


# --- the roots the pipeline resolves data against ------------------------------------------------


def test_the_pipeline_s_repo_root_is_the_checkout_not_its_parent() -> None:
    """Every module that resolves data against `REPO_ROOT` must agree that it is the checkout itself.

    A root taken as the package's parent points one level above the checkout, and the `data` phase then
    looks for its inputs outside the repository.
    """
    from pipeline.cli import REPO_ROOT as cli_root
    from pipeline.launcher import REPO_ROOT as launcher_root
    from pipeline.orchestrator import REPO_ROOT as orchestrator_root

    for name, root in (("orchestrator", orchestrator_root), ("cli", cli_root), ("launcher", launcher_root)):
        assert (root / "configs" / "pipeline.yaml").is_file(), f"{name}: no configs/ under {root}"
        assert (root / "run.sh").is_file(), f"{name}: no run.sh under {root}"
        assert root == REPO_ROOT, f"{name}: {root} is not the checkout"


def test_every_declared_data_source_resolves_under_the_checkout() -> None:
    """Resolution, not existence: `data/external/` is git-ignored and absent on a fresh clone, which the
    `data` phase reports as a missing source. What must hold is that it looks inside the repository."""
    import yaml

    from pipeline.orchestrator import REPO_ROOT as root
    from tools.data.local_sources import LocalSource, resolve

    config = yaml.safe_load((REPO_ROOT / "configs" / "pipeline.yaml").read_text())
    declared = config["phases"]["data"]["sources"]
    sources = [
        LocalSource(
            id=s["id"],
            path=Path(s["path"]),
            kind=s.get("kind", "table"),
            license_note=s.get("license_note", ""),
            required=s.get("required", True),
        )
        for s in declared
    ]
    for source in resolve(sources, root):
        assert str(source.path).startswith(str(REPO_ROOT)), f"{source.id} resolves outside: {source.path}"


def test_every_declared_data_source_says_how_to_obtain_it() -> None:
    """`data` is where a fresh clone stops, so the message it prints is a user-facing document: it reads the
    `acquire` field of each declaration. A source without one is a gap in the configuration."""
    import yaml

    for name in ("pipeline.yaml", "pipeline-smoke.yaml", "pipeline-probe-outside-main-text.yaml"):
        config = yaml.safe_load((REPO_ROOT / "configs" / name).read_text())
        for source in config["phases"]["data"]["sources"]:
            assert source.get("acquire"), f"{name}: {source['id']} declares no acquisition procedure"


def test_every_declared_acquisition_command_can_be_pasted_as_is() -> None:
    """A `run` step is printed after `$ ` for a user to paste. It must name an executable script of this
    repository and carry no prose: shell punctuation in prose would run its words as commands."""
    import shlex

    import yaml

    for name in ("pipeline.yaml", "pipeline-smoke.yaml", "pipeline-probe-outside-main-text.yaml"):
        config = yaml.safe_load((REPO_ROOT / "configs" / name).read_text())
        for source in config["phases"]["data"]["sources"]:
            for step in source["acquire"]["steps"]:
                if "run" not in step:
                    continue
                argv = shlex.split(step["run"])
                script = REPO_ROOT / argv[0]
                assert script.is_file() and os.access(script, os.X_OK), f"{name}: {step['run']!r} is not runnable"
                assert not set(";&|(),").intersection(step["run"]), f"{name}: {step['run']!r} carries prose"


def test_the_paths_the_launcher_hands_a_session_are_inside_the_checkout() -> None:
    """The result extractor and the paper extraction the launcher hands a session resolve under the
    checkout, not one directory above it."""
    from pipeline.launcher import EXTRACTION
    from pipeline.launcher import REPO_ROOT as root

    assert (root / "pipeline" / "extract_workflow_result.py").is_file()
    assert str(EXTRACTION).startswith(str(REPO_ROOT)), EXTRACTION


def test_the_run_scorer_defaults_to_the_knowledge_base_in_the_checkout() -> None:
    from evaluation.run_score import DEFAULT_KB

    assert str(DEFAULT_KB).startswith(str(REPO_ROOT)), DEFAULT_KB
