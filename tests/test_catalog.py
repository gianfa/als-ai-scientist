"""RunCatalog: the single answer to "which runs exist, and where does each one live".

These tests cover run-identity defects: a run id that
one view offers and another rejects (`my-run-002` listed by `/runs`, 404 by `/tokens`), a run
listed twice because the store and `experiments/` each discovered it, a directory that is not a run
yet counted as one, a traversal attempt reaching the filesystem, and a state probe whose failure
takes the whole listing down with it.
"""

import json
import os
from pathlib import Path

import pytest

from observability.wf_observer.catalog import RunCatalog, RunRef, is_safe_name
from observability.wf_observer.store import RunStore

# Store ids must match events.RUN_ID_PATTERN (^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$).
STORE_RUN_A = "20260928T204319Z-3e5caeef"
STORE_RUN_B = "20260929T101500Z-aabbccdd"


def _experiment(experiments_root: Path, run_id: str, *, evaluated: bool = True, mtime: float | None = None) -> Path:
    """An experiment as run.sh leaves it: <root>/<id>/results with an evaluation or a context."""
    results = experiments_root / run_id / "results"
    results.mkdir(parents=True)
    if evaluated:
        (results / "evaluation").mkdir()
        marker = results / "evaluation" / "evaluation.json"
        marker.write_text(json.dumps({"structural_status": "PASS"}))
    else:
        marker = results / "context.json"
        marker.write_text(json.dumps({"started_utc": "2026-09-28T20:43:19Z"}))
    if mtime is not None:
        os.utime(marker, (mtime, mtime))
    return results


def _smoke(smoke_root: Path, run_id: str) -> Path:
    run_dir = smoke_root / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "summary.json").write_text(json.dumps({"workflow_status": "completed"}))
    return run_dir


def _catalog(tmp_path: Path, *, store: RunStore | None = None, state_fn=None) -> RunCatalog:
    """A catalog whose three roots are all under tmp_path, so no test reads the real repository."""
    return RunCatalog(
        experiments_root=tmp_path / "experiments",
        store=store,
        smoke_root=tmp_path / "smoke",
        state_fn=state_fn,
    )


# -- id validation ----------------------------------------------------------


@pytest.mark.parametrize("name", ["e2e-015", "my-run-002", "20260928T204319Z-3e5caeef", "a", "run.2", "RUN_3"])
def test_is_safe_name_accepts_every_run_id_the_launcher_can_produce(name: str) -> None:
    assert is_safe_name(name) is True


@pytest.mark.parametrize(
    "name", ["", ".", "..", "a/b", "../../etc", "/e2e-015", "e2e 015", "e2e-015 ", "e2e\n015", "e2e-015/", "a\\b"]
)
def test_is_safe_name_rejects_anything_that_is_not_an_inert_path_segment(name: str) -> None:
    assert is_safe_name(name) is False


# -- listing ----------------------------------------------------------------


def test_list_returns_experiments_newest_first(tmp_path: Path) -> None:
    root = tmp_path / "experiments"
    _experiment(root, "e2e-001", mtime=1_700_000_001)
    _experiment(root, "e2e-002", mtime=1_700_000_003)
    _experiment(root, "e2e-003", mtime=1_700_000_002)

    assert [ref.id for ref in _catalog(tmp_path).list()] == ["e2e-002", "e2e-003", "e2e-001"]


def test_list_breaks_a_tie_on_started_at_by_the_higher_id_first(tmp_path: Path) -> None:
    """Ties are not hypothetical: runs written in one burst share an mtime to the filesystem's
    precision. Run ids are timestamps often enough that the higher id is the later run, so the tie
    must resolve the same way as the primary key — newest first."""
    root = tmp_path / "experiments"
    for run_id in ("e2e-003", "e2e-001", "e2e-002"):
        _experiment(root, run_id, mtime=1_700_000_000)

    assert [ref.id for ref in _catalog(tmp_path).list()] == ["e2e-003", "e2e-002", "e2e-001"]


def test_an_experiment_directory_without_a_context_or_an_evaluation_is_not_a_run_yet(tmp_path: Path) -> None:
    (tmp_path / "experiments" / "e2e-004" / "results").mkdir(parents=True)  # died before writing anything
    _experiment(tmp_path / "experiments", "e2e-005")

    assert [ref.id for ref in _catalog(tmp_path).list()] == ["e2e-005"]


def test_an_experiment_with_only_a_context_is_listed(tmp_path: Path) -> None:
    """A live run has no evaluation yet; it must still be selectable."""
    _experiment(tmp_path / "experiments", "e2e-006", evaluated=False)

    [ref] = _catalog(tmp_path).list()
    assert ref.id == "e2e-006" and ref.source == "experiment" and ref.started_at > 0


def test_an_experiment_named_by_a_store_run_is_listed_once_through_the_store(tmp_path: Path) -> None:
    _experiment(tmp_path / "experiments", "e2e-001")
    _experiment(tmp_path / "experiments", "e2e-002")
    store = RunStore(tmp_path / "runs")
    store.create_run(STORE_RUN_A, {"name": "e2e-001"})

    refs = _catalog(tmp_path, store=store).list()
    assert sorted(ref.id for ref in refs) == sorted([STORE_RUN_A, "e2e-002"])
    [store_ref] = [ref for ref in refs if ref.id == STORE_RUN_A]
    assert store_ref.source == "store"
    # The one entry points at the experiment's artifacts, not at the bare store directory.
    assert store_ref.results_dir == tmp_path / "experiments" / "e2e-001" / "results"


def test_list_excludes_smoke_runs_by_default_and_includes_them_on_request(tmp_path: Path) -> None:
    _experiment(tmp_path / "experiments", "e2e-001")
    _smoke(tmp_path / "smoke", "20260929T120000Z")

    catalog = _catalog(tmp_path)
    assert [ref.id for ref in catalog.list()] == ["e2e-001"]

    with_smoke = catalog.list(include_smoke=True)
    assert sorted(ref.id for ref in with_smoke) == ["20260929T120000Z", "e2e-001"]
    assert {ref.source for ref in with_smoke} == {"smoke", "experiment"}


def test_a_smoke_id_that_is_already_listed_is_not_added_a_second_time(tmp_path: Path) -> None:
    _experiment(tmp_path / "experiments", "e2e-015")
    _smoke(tmp_path / "smoke", "e2e-015")

    refs = _catalog(tmp_path).list(include_smoke=True)
    assert [ref.id for ref in refs] == ["e2e-015"]
    assert refs[0].source == "experiment"


def test_list_is_empty_when_no_root_exists(tmp_path: Path) -> None:
    assert _catalog(tmp_path).list(include_smoke=True) == []


# -- resolution -------------------------------------------------------------


def test_get_resolves_an_experiment_id(tmp_path: Path) -> None:
    results = _experiment(tmp_path / "experiments", "e2e-011")

    ref = _catalog(tmp_path).get("e2e-011")
    assert ref is not None
    assert (ref.id, ref.display, ref.source, ref.results_dir) == ("e2e-011", "e2e-011", "experiment", results)


def test_get_resolves_a_store_id(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs")
    store.create_run(STORE_RUN_A, {"name": "e2e-001"})

    ref = _catalog(tmp_path, store=store).get(STORE_RUN_A)
    assert ref is not None and ref.id == STORE_RUN_A and ref.source == "store"


def test_get_resolves_a_smoke_id_only_when_smoke_runs_are_included(tmp_path: Path) -> None:
    run_dir = _smoke(tmp_path / "smoke", "20260929T120000Z")
    catalog = _catalog(tmp_path)

    ref = catalog.get("20260929T120000Z")
    assert ref is not None and ref.source == "smoke" and ref.results_dir == run_dir
    assert catalog.get("20260929T120000Z", include_smoke=False) is None


def test_get_returns_none_for_an_unknown_id(tmp_path: Path) -> None:
    _experiment(tmp_path / "experiments", "e2e-011")

    assert _catalog(tmp_path).get("e2e-777") is None


@pytest.mark.parametrize("attempt", ["../../etc", "..", "a/b", "/etc/passwd", ""])
def test_get_refuses_a_traversal_attempt_without_touching_the_filesystem(tmp_path: Path, attempt: str) -> None:
    _experiment(tmp_path / "experiments", "e2e-011")

    assert _catalog(tmp_path).get(attempt) is None


# -- free-text run ids ------------------------------------------------------


def test_a_free_text_run_id_rejected_by_the_old_patterns_resolves_and_is_listed(tmp_path: Path) -> None:
    """A free-text `RUN_ID=my-run-002` (docs/quick-start.md) must not be rejected by `^e2e-\\d{3}$`."""
    results = _experiment(tmp_path / "experiments", "my-run-002")
    catalog = _catalog(tmp_path)

    ref = catalog.get("my-run-002")
    assert ref is not None
    assert ref.results_dir == tmp_path / "experiments" / "my-run-002" / "results" == results
    assert "my-run-002" in [listed.id for listed in catalog.list()]


# -- state probe injection --------------------------------------------------


def test_an_injected_state_fn_decides_status_and_finished(tmp_path: Path) -> None:
    results = _experiment(tmp_path / "experiments", "e2e-011")
    seen: list[Path] = []

    def probe(results_dir: Path) -> tuple[str, bool]:
        seen.append(results_dir)
        return "failed", True

    ref = _catalog(tmp_path, state_fn=probe).get("e2e-011")
    assert ref is not None and (ref.status, ref.finished) == ("failed", True)
    assert seen == [results]
    assert ref.to_dict() == {
        "id": "e2e-011",
        "display": "e2e-011",
        "source": "experiment",
        "status": "failed",
        "finished": True,
    }


def test_without_a_state_fn_every_run_reads_as_unknown_rather_than_failing(tmp_path: Path) -> None:
    _experiment(tmp_path / "experiments", "e2e-011")

    ref = _catalog(tmp_path).get("e2e-011")
    assert ref is not None and (ref.status, ref.finished) == ("unknown", False)


def test_a_state_fn_that_raises_degrades_to_unknown_instead_of_breaking_the_listing(tmp_path: Path) -> None:
    _experiment(tmp_path / "experiments", "e2e-011")

    def broken(_: Path) -> tuple[str, bool]:
        raise OSError("probe exploded")

    refs = _catalog(tmp_path, state_fn=broken).list()
    assert [(ref.id, ref.status, ref.finished) for ref in refs] == [("e2e-011", "unknown", False)]


# -- store display ----------------------------------------------------------


def test_a_store_run_with_a_workflow_id_is_displayed_as_name_and_workflow_id(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs")
    store.create_run(STORE_RUN_A, {"name": "e2e-001", "workflow_run_id": "wf_abc"})

    ref = _catalog(tmp_path, store=store).get(STORE_RUN_A)
    assert ref is not None and ref.display == "e2e-001 | wf_abc"


def test_a_store_run_without_a_workflow_id_is_displayed_by_its_name_alone(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs")
    store.create_run(STORE_RUN_B, {"name": "e2e-002"})

    ref = _catalog(tmp_path, store=store).get(STORE_RUN_B)
    assert ref is not None and ref.display == "e2e-002"


def test_a_store_run_with_an_unreadable_context_falls_back_to_its_directory_name(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs")
    run_dir = store.create_run(STORE_RUN_A, {"name": "e2e-001"})
    (run_dir / "context.json").write_text("{ not json")

    ref = _catalog(tmp_path, store=store).get(STORE_RUN_A)
    assert ref is not None
    assert ref.display == STORE_RUN_A
    # With no context there is no experiment to point at, so the store directory is the artifact root.
    assert ref.results_dir == run_dir


def test_a_store_run_naming_no_experiment_keeps_its_own_directory_as_results_dir(tmp_path: Path) -> None:
    (tmp_path / "experiments").mkdir()
    store = RunStore(tmp_path / "runs")
    run_dir = store.create_run(STORE_RUN_A, {"name": "test-run"})

    ref = _catalog(tmp_path, store=store).get(STORE_RUN_A)
    assert ref is not None and ref.display == "test-run" and ref.results_dir == run_dir


def test_a_store_run_whose_name_is_a_traversal_attempt_never_leaves_the_experiments_root(tmp_path: Path) -> None:
    (tmp_path / "experiments").mkdir()
    store = RunStore(tmp_path / "runs")
    run_dir = store.create_run(STORE_RUN_A, {"name": "../.."})

    ref = _catalog(tmp_path, store=store).get(STORE_RUN_A)
    assert ref is not None and ref.results_dir == run_dir


def test_a_run_ref_is_frozen_so_a_view_cannot_rewrite_a_resolved_run(tmp_path: Path) -> None:
    ref = RunRef("e2e-011", "e2e-011", "experiment", tmp_path / "results")
    with pytest.raises(AttributeError):
        ref.id = "e2e-012"  # type: ignore[misc]
