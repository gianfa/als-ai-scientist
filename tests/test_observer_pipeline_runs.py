"""The observer on runs of the phase pipeline: per-phase stages, liveness from `run.pid`, and the scorecard as the
Evaluation view. Synthetic archives only: the real ones under experiments/ are git-ignored."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from observability.wf_observer.server import (
    _run_state,
    is_run_in_progress,
    make_handler,
    run_stages,
    scan_running_runs,
)
from observability.wf_observer.store import RunStore
from pipeline.stop import RUN_PID_FILE, write_pid_file

PHASES = ["data", "compute", "literature", "generate"]


def archive(root: Path, run_id: str, completed: list[str], kb: Path | None = None) -> Path:
    results = root / run_id / "results"
    results.mkdir(parents=True)
    context = {"run_id": run_id, "phases": PHASES, "kb": str(kb) if kb else None}
    (results / "context.json").write_text(json.dumps(context))
    (results / "state.json").write_text(json.dumps({"run_id": run_id, "completed": completed, "phases": {}}))
    return results


@pytest.fixture
def orchestrator() -> Iterator[subprocess.Popen]:
    """A live process whose command line names the run, as the real orchestrator's does."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "--run-id", "pl-live"])
    try:
        yield proc
    finally:
        proc.kill()
        proc.wait()


def statuses(results: Path) -> list[tuple[str, str]]:
    return [(s.stage, s.status) for s in run_stages(results)]


def test_a_finished_run_shows_every_phase_passed(tmp_path: Path) -> None:
    results = archive(tmp_path, "pl-done", PHASES)
    assert statuses(results) == [(p, "passed") for p in PHASES]
    assert _run_state(results) == ("completed", True)


def test_an_ended_incomplete_run_marks_where_it_stopped(tmp_path: Path) -> None:
    results = archive(tmp_path, "pl-stopped", ["data", "compute"])
    stages = run_stages(results)
    assert [(s.stage, s.status) for s in stages] == [
        ("data", "passed"),
        ("compute", "passed"),
        ("literature", "failed"),
        ("generate", "not_started"),
    ]
    assert stages[2].detail == "the run ended before this phase completed"
    assert _run_state(results) == ("incomplete", True)


def test_a_live_run_is_running_at_its_next_phase(tmp_path: Path, orchestrator: subprocess.Popen) -> None:
    results = archive(tmp_path, "pl-live", ["data"])
    write_pid_file(results, RUN_PID_FILE, "orchestrator", orchestrator.pid, None)
    assert statuses(results)[1] == ("compute", "running")
    assert _run_state(results) == ("running", False)
    context = json.loads((results / "context.json").read_text())
    assert is_run_in_progress(results, context)
    assert [r["run_id"] for r in scan_running_runs(tmp_path)] == ["pl-live"]


def test_a_recorded_pid_that_belongs_to_another_process_is_not_a_live_run(
    tmp_path: Path, orchestrator: subprocess.Popen
) -> None:
    results = archive(tmp_path, "pl-other", ["data"])  # the live process names pl-live, not pl-other
    write_pid_file(results, RUN_PID_FILE, "orchestrator", orchestrator.pid, None)
    assert _run_state(results) == ("incomplete", True)
    assert scan_running_runs(tmp_path) == []


def serve(tmp_path: Path) -> tuple[ThreadingHTTPServer, str]:
    store = RunStore(tmp_path / "store")
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), make_handler(store, experiments_root=tmp_path / "experiments", smoke_root=tmp_path / "smoke")
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def test_the_evaluation_of_a_pipeline_run_is_its_scorecard(tmp_path: Path) -> None:
    kb = tmp_path / "kb"
    kb.mkdir()
    archive(tmp_path / "experiments", "pl-eval", PHASES, kb=kb)
    server, base = serve(tmp_path)
    try:
        with urllib.request.urlopen(f"{base}/evaluation/pl-eval") as r:
            data = json.loads(r.read())
        assert data["kind"] == "scorecard"
        assert data["run_id"] == "pl-eval"
        assert data["rows"] and {"id", "dimension", "value", "status", "from", "limit"} <= set(data["rows"][0])
        assert sum(data["counts"].values()) == len(data["rows"])
    finally:
        server.shutdown()
        server.server_close()


def test_a_missing_knowledge_base_is_reported_not_scored(tmp_path: Path) -> None:
    archive(tmp_path / "experiments", "pl-nokb", PHASES, kb=tmp_path / "absent")
    server, base = serve(tmp_path)
    try:
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(f"{base}/evaluation/pl-nokb")
        assert err.value.code == 404
        assert "knowledge base" in json.loads(err.value.read())["error"]
    finally:
        server.shutdown()
        server.server_close()
