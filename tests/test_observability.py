import json
import os
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from observability.wf_observer._config import ARTIFACTS_ROOT
from observability.wf_observer.cli import main
from observability.wf_observer.events import Event, InvalidEventError, new_run_id
from observability.wf_observer.metrics import summarize
from observability.wf_observer.server import make_handler
from observability.wf_observer.store import RunNotFoundError, RunStore


@pytest.fixture
def store(tmp_path: Path) -> RunStore:
    return RunStore(tmp_path / "runs")


@pytest.fixture
def run_id(store: RunStore) -> str:
    rid = new_run_id()
    store.create_run(rid, {"name": "test"})
    return rid


def test_artifacts_root_is_absolute_and_not_doubled() -> None:
    """ARTIFACTS_ROOT is absolute and ends at the correct level.

    A cwd-relative Path("artifacts") would create the directory wherever serve() was started.
    """
    assert ARTIFACTS_ROOT.is_absolute(), f"ARTIFACTS_ROOT must be absolute, got: {ARTIFACTS_ROOT}"

    # Verify the path ends with exactly "artifacts" (one level)
    assert ARTIFACTS_ROOT.name == "artifacts", f"ARTIFACTS_ROOT name should be 'artifacts', got: {ARTIFACTS_ROOT.name}"

    # ARTIFACTS_ROOT sits directly in the repository root.
    assert (
        ARTIFACTS_ROOT.parent / "observability" / "wf_observer" / "_config.py"
    ).is_file(), f"ARTIFACTS_ROOT does not sit in the repository root: {ARTIFACTS_ROOT}"
    assert (
        ARTIFACTS_ROOT.parent / "pyproject.toml"
    ).is_file(), f"ARTIFACTS_ROOT's parent is not the repository root: {ARTIFACTS_ROOT.parent}"


def test_event_rejects_unknown_kind_and_negative_values(run_id: str) -> None:
    with pytest.raises(InvalidEventError):
        Event(run_id=run_id, kind="made_up")
    with pytest.raises(InvalidEventError):
        Event(run_id=run_id, kind="agent_finished", latency_ms=-1)
    with pytest.raises(InvalidEventError):
        Event(run_id="../etc", kind="error")


def test_event_from_dict_rejects_unknown_fields(run_id: str) -> None:
    raw = Event(run_id=run_id, kind="error").to_dict() | {"surprise": 1}
    with pytest.raises(InvalidEventError):
        Event.from_dict(raw)


def test_store_roundtrip_and_never_overwrites(store: RunStore, run_id: str) -> None:
    event = Event(run_id=run_id, kind="agent_finished", agent="critic", latency_ms=10)
    store.append(event)
    assert store.read_events(run_id) == [event]
    assert store.read_context(run_id)["name"] == "test"
    with pytest.raises(FileExistsError):
        store.create_run(run_id, {})


def test_store_fails_loudly_on_malformed_line(store: RunStore, run_id: str) -> None:
    with (store.run_dir(run_id) / "events.jsonl").open("a") as f:
        f.write('{"run_id": "x"}\n')
    with pytest.raises(InvalidEventError):
        store.read_events(run_id)


def test_append_to_missing_run_fails(store: RunStore) -> None:
    with pytest.raises(RunNotFoundError):
        store.append(Event(run_id=new_run_id(), kind="error"))


def test_metrics_missing_values_are_none_not_zero(run_id: str) -> None:
    m = summarize(run_id, [Event(run_id=run_id, kind="agent_finished", agent="pi")])
    assert m.agent_calls == 1
    assert m.cost_usd is None and m.latency_ms_median is None and m.input_tokens is None


def test_metrics_aggregates(run_id: str) -> None:
    events = [
        Event(run_id=run_id, kind="agent_finished", agent="critic", latency_ms=10, input_tokens=5, cost_usd=0.5),
        Event(run_id=run_id, kind="agent_finished", agent="critic", latency_ms=30, input_tokens=7),
        Event(run_id=run_id, kind="gate", data={"gate": "G3", "outcome": "rejected"}),
        Event(run_id=run_id, kind="error"),
        Event(run_id=run_id, kind="run_finished"),
    ]
    m = summarize(run_id, events)
    assert m.agent_calls_by_agent == {"critic": 2}
    assert m.latency_ms_median == 20 and m.latency_ms_max == 30
    assert m.input_tokens == 12 and m.cost_usd == 0.5
    assert m.gates == {"G3:rejected": 1} and m.errors == 1 and m.finished


def test_cli_start_log_metrics(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = ["--root", str(tmp_path)]
    assert main(root + ["start", "--name", "smoke"]) == 0
    rid = capsys.readouterr().out.strip()
    assert main(root + ["log", rid, "agent_finished", "--agent", "pi", "--latency-ms", "5"]) == 0
    assert main(root + ["log", rid, "agent_finished", "--data", "not json"]) == 1
    capsys.readouterr()
    assert main(root + ["metrics", rid]) == 0
    assert json.loads(capsys.readouterr().out)["agent_calls"] == 1


def test_server_serves_metrics_and_404(store: RunStore, run_id: str, tmp_path: Path) -> None:
    # Isolate experiments_root: /runs also lists experiments, so the default would read the
    # repository's real experiments/ directory and make this assertion depend on it.
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(store, experiments_root=tmp_path / "experiments"))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with urllib.request.urlopen(f"{base}/runs") as r:
            # Rows are enriched for the selector, so assert the identity and that the
            # added figures are present rather than pinning the whole shape.
            rows = json.load(r)
            assert [(row["id"], row["display"], row["source"]) for row in rows] == [(run_id, "test", "store")]
            assert {"status", "finished", "cost_usd", "duration_ms", "total_tokens"} <= rows[0].keys()
        with urllib.request.urlopen(f"{base}/runs/{run_id}/metrics") as r:
            assert json.load(r)["events"] == 0
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(f"{base}/runs/..%2F..%2Fetc/events")
        assert exc.value.code == 404
    finally:
        server.shutdown()
        server.server_close()


def test_server_serves_ui(store: RunStore) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(store))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with urllib.request.urlopen(f"{base}/") as r:
            assert r.headers["Content-Type"].startswith("text/html")
            page = r.read().decode()
            # One menu item: the run is a global selection, not a tab.
            assert 'data-tab="run"' in page and 'data-tab="agents"' not in page
            assert 'id="run-select"' in page
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(f"{base}/card")
        assert err.value.code == 404
    finally:
        server.shutdown()
        server.server_close()


def _write_evaluation(experiments_root: Path, experiment_id: str, status: str) -> None:
    eval_dir = experiments_root / experiment_id / "results" / "evaluation"
    eval_dir.mkdir(parents=True)
    (eval_dir / "evaluation.json").write_text(json.dumps({"structural_status": status}))


def test_evaluation_is_resolved_per_run_experiment(store: RunStore, tmp_path: Path) -> None:
    """Each run resolves to its own experiment; a run without one must 404, not fall back."""
    experiments_root = tmp_path / "experiments"
    _write_evaluation(experiments_root, "e2e-001", "PASS")
    _write_evaluation(experiments_root, "e2e-002", "FAIL")

    run_a, run_b, orphan = new_run_id(), new_run_id(), new_run_id()
    store.create_run(run_a, {"name": "e2e-001"})
    store.create_run(run_b, {"name": "e2e-002"})
    store.create_run(orphan, {"name": "test-run"})

    handler = make_handler(store, smoke_root=tmp_path / "smoke", experiments_root=experiments_root)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with urllib.request.urlopen(f"{base}/evaluation/{run_a}") as r:
            assert json.load(r)["structural_status"] == "PASS"
        with urllib.request.urlopen(f"{base}/evaluation/{run_b}") as r:
            assert json.load(r)["structural_status"] == "FAIL"
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(f"{base}/evaluation/{orphan}")
        assert exc.value.code == 404
    finally:
        server.shutdown()
        server.server_close()


def test_evaluation_rejects_traversal_experiment_name(store: RunStore, tmp_path: Path) -> None:
    experiments_root = tmp_path / "experiments"
    experiments_root.mkdir()
    (tmp_path / "evaluation").mkdir()
    (tmp_path / "evaluation" / "evaluation.json").write_text('{"structural_status": "LEAKED"}')

    run_id = new_run_id()
    store.create_run(run_id, {"name": "../.."})

    handler = make_handler(store, smoke_root=tmp_path / "smoke", experiments_root=experiments_root)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(f"http://127.0.0.1:{server.server_address[1]}/evaluation/{run_id}")
        assert exc.value.code == 404
    finally:
        server.shutdown()
        server.server_close()


def _serve(handler) -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def test_runs_lists_experiments_written_by_the_launcher(store: RunStore, tmp_path: Path) -> None:
    """run.sh writes an experiment but registers no store run; it must still be selectable."""
    experiments_root = tmp_path / "experiments"
    for experiment_id in ("e2e-001", "e2e-002", "e2e-003"):
        _write_evaluation(experiments_root, experiment_id, "FAIL")
    # Make the third run the most recently evaluated.
    for offset, experiment_id in enumerate(("e2e-001", "e2e-002", "e2e-003"), start=1):
        path = experiments_root / experiment_id / "results" / "evaluation" / "evaluation.json"
        os.utime(path, (1_700_000_000 + offset, 1_700_000_000 + offset))

    server, base = _serve(make_handler(store, smoke_root=tmp_path / "smoke", experiments_root=experiments_root))
    try:
        with urllib.request.urlopen(f"{base}/runs") as r:
            runs = json.load(r)
        assert [run["id"] for run in runs] == ["e2e-003", "e2e-002", "e2e-001"]  # newest first
        # Each id resolves to its own evaluation, with no store run to go through.
        with urllib.request.urlopen(f"{base}/evaluation/e2e-003") as r:
            assert json.load(r)["structural_status"] == "FAIL"
    finally:
        server.shutdown()
        server.server_close()


def test_an_experiment_named_by_a_store_run_is_not_listed_twice(store: RunStore, tmp_path: Path) -> None:
    experiments_root = tmp_path / "experiments"
    _write_evaluation(experiments_root, "e2e-001", "PASS")
    _write_evaluation(experiments_root, "e2e-002", "FAIL")
    run_id = new_run_id()
    store.create_run(run_id, {"name": "e2e-001", "workflow_run_id": "wf_abc"})

    server, base = _serve(make_handler(store, smoke_root=tmp_path / "smoke", experiments_root=experiments_root))
    try:
        with urllib.request.urlopen(f"{base}/runs") as r:
            runs = json.load(r)
        ids = [run["id"] for run in runs]
        assert sorted(ids) == sorted([run_id, "e2e-002"])  # the first run only via its store run
        assert [run["display"] for run in runs if run["id"] == run_id] == ["e2e-001 | wf_abc"]
    finally:
        server.shutdown()
        server.server_close()


def test_an_experiment_without_an_evaluation_is_not_listed(store: RunStore, tmp_path: Path) -> None:
    experiments_root = tmp_path / "experiments"
    (experiments_root / "e2e-004" / "results").mkdir(parents=True)  # run died before evaluation

    server, base = _serve(make_handler(store, smoke_root=tmp_path / "smoke", experiments_root=experiments_root))
    try:
        with urllib.request.urlopen(f"{base}/runs") as r:
            assert json.load(r) == []
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(f"{base}/evaluation/e2e-004")
        assert exc.value.code == 404
    finally:
        server.shutdown()
        server.server_close()


def test_evaluation_by_experiment_id_rejects_traversal(store: RunStore, tmp_path: Path) -> None:
    experiments_root = tmp_path / "experiments"
    experiments_root.mkdir()
    (tmp_path / "evaluation").mkdir()
    (tmp_path / "evaluation" / "evaluation.json").write_text('{"structural_status": "LEAKED"}')

    server, base = _serve(make_handler(store, smoke_root=tmp_path / "smoke", experiments_root=experiments_root))
    try:
        for attempt in ("..", "..%2F..", "%2Fetc%2Fpasswd"):
            with pytest.raises(urllib.error.HTTPError) as exc:
                urllib.request.urlopen(f"{base}/evaluation/{attempt}")
            assert exc.value.code == 404
    finally:
        server.shutdown()
        server.server_close()


# Tests for tokens endpoint enhancements


def test_smoke_run_with_completed_summary_is_finished_without_tokens_json(tmp_path: Path) -> None:
    """A smoke run with completed status in summary.json and no tokens.json should be marked finished."""
    smoke_root = tmp_path / "smoke"
    smoke_root.mkdir()
    run_id = "20260929T120000Z"
    run_dir = smoke_root / run_id
    run_dir.mkdir()

    # Write summary with completed status, but no tokens.json
    summary_file = run_dir / "summary.json"
    summary_file.write_text(json.dumps({"workflow_status": "completed"}))

    handler = make_handler(RunStore(tmp_path / "runs"), smoke_root=smoke_root, e2e_root=tmp_path / "e2e")
    server, base = _serve(handler)
    try:
        with urllib.request.urlopen(f"{base}/tokens") as r:
            runs = json.load(r)
        assert len(runs) == 1
        assert runs[0]["run_id"] == run_id
        assert runs[0]["kind"] == "smoke"
        assert runs[0]["finished"] is True
        assert runs[0]["status"] == "completed"
        assert runs[0]["has_saved"] is False
    finally:
        server.shutdown()
        server.server_close()


def test_e2e_run_listed_with_kind_and_status_from_run_exit_code(tmp_path: Path) -> None:
    """E2E run with run_exit_code is listed with kind='e2e' and status inferred from exit code."""
    e2e_root = tmp_path / "e2e"
    run_dir = e2e_root / "e2e-005" / "results"
    run_dir.mkdir(parents=True)

    # Write run_exit_code (0 = completed, nonzero = failed)
    (run_dir / "run_exit_code").write_text("0")

    handler = make_handler(RunStore(tmp_path / "runs"), smoke_root=tmp_path / "smoke", e2e_root=e2e_root)
    server, base = _serve(handler)
    try:
        with urllib.request.urlopen(f"{base}/tokens") as r:
            runs = json.load(r)
        e2e_runs = [r for r in runs if r["kind"] == "e2e"]
        assert len(e2e_runs) == 1
        assert e2e_runs[0]["run_id"] == "e2e-005"
        assert e2e_runs[0]["status"] == "completed"
        assert e2e_runs[0]["finished"] is True
    finally:
        server.shutdown()
        server.server_close()


def test_invalid_e2e_id_rejected_with_404(tmp_path: Path) -> None:
    """Invalid e2e IDs like 'e2e-../x' or 'e2e-1' are rejected with 404."""
    e2e_root = tmp_path / "e2e"
    e2e_root.mkdir()

    handler = make_handler(RunStore(tmp_path / "runs"), smoke_root=tmp_path / "smoke", e2e_root=e2e_root)
    server, base = _serve(handler)
    try:
        for bad_id in ("e2e-1", "e2e-01", "e2e-0001", "../e2e-005", "e2e-x"):
            with pytest.raises(urllib.error.HTTPError) as exc:
                urllib.request.urlopen(f"{base}/tokens/{bad_id}?mode=live")
            assert exc.value.code == 404, f"Expected 404 for {bad_id}, got {exc.value.code}"
    finally:
        server.shutdown()
        server.server_close()


def test_legacy_flag_added_to_saved_snapshot_without_schema_version(tmp_path: Path) -> None:
    """A saved tokens.json without schema_version gets legacy: true added to the response."""
    smoke_root = tmp_path / "smoke"
    smoke_root.mkdir()
    run_id = "20260929T120000Z"
    run_dir = smoke_root / run_id
    run_dir.mkdir()

    # Write legacy tokens.json without schema_version
    tokens_file = run_dir / "tokens.json"
    legacy_data = {
        "totals": {"input": {"total": 100}, "output": 50, "est_usd": 0.5},
        "by_role": {},
        "by_agent": {},
        "series": [],
        "limits": {},
        "status": "running",
        "source": "saved",
        "generated_at": "2026-09-29T12:00:00Z",
    }
    tokens_file.write_text(json.dumps(legacy_data))

    handler = make_handler(RunStore(tmp_path / "runs"), smoke_root=smoke_root, e2e_root=tmp_path / "e2e")
    server, base = _serve(handler)
    try:
        with urllib.request.urlopen(f"{base}/tokens/{run_id}?mode=saved") as r:
            data = json.load(r)
        assert data.get("legacy") is True
        assert "schema_version" not in data  # We don't modify the file
    finally:
        server.shutdown()
        server.server_close()


def test_e2e_live_aggregates_multiple_stream_files(tmp_path: Path) -> None:
    """E2E live mode with discovery and ca output files aggregates both."""
    e2e_root = tmp_path / "e2e"
    run_dir = e2e_root / "e2e-005" / "results"
    run_dir.mkdir(parents=True)

    # Write minimal discovery output (orchestrator usage)
    discovery_output = run_dir / "discovery-output.jsonl"
    discovery_output.write_text(
        json.dumps(
            {
                "type": "assistant",
                "timestamp": "2026-09-29T12:00:00Z",
                "message": {
                    "id": "msg-1",
                    "model": "claude-haiku-4-5-20251001",
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 50,
                        "cache_creation_input_tokens_5m": 0,
                        "cache_creation_input_tokens_1h": 0,
                        "cache_read_input_tokens": 0,
                    },
                },
            }
        )
        + "\n"
    )

    # Write minimal ca output (orchestrator usage)
    ca_output = run_dir / "ca-output.jsonl"
    ca_output.write_text(
        json.dumps(
            {
                "type": "assistant",
                "timestamp": "2026-09-29T12:01:00Z",
                "message": {
                    "id": "msg-2",
                    "model": "claude-haiku-4-5-20251001",
                    "usage": {
                        "input_tokens": 200,
                        "output_tokens": 100,
                        "cache_creation_input_tokens_5m": 0,
                        "cache_creation_input_tokens_1h": 0,
                        "cache_read_input_tokens": 0,
                    },
                },
            }
        )
        + "\n"
    )

    # Create resolved config (needed for aggregate)
    (run_dir / "resolved_config.json").write_text(json.dumps({"agents": {}}))

    handler = make_handler(RunStore(tmp_path / "runs"), smoke_root=tmp_path / "smoke", e2e_root=e2e_root)
    server, base = _serve(handler)
    try:
        with urllib.request.urlopen(f"{base}/tokens/e2e-005?mode=live") as r:
            data = json.load(r)
        # Check that both streams were aggregated
        totals = data.get("totals", {})
        # Should have sum of both streams' inputs: 100 + 200 = 300
        assert totals.get("input", {}).get("total", 0) >= 300, f"Expected at least 300 input tokens, got {totals}"
        # Should have sum of both streams' outputs: 50 + 100 = 150
        assert totals.get("output", 0) >= 150, f"Expected at least 150 output tokens, got {totals}"
    finally:
        server.shutdown()
        server.server_close()


def _list_tokens_e2e(tmp_path: Path, e2e_root: Path) -> list[dict]:
    handler = make_handler(RunStore(tmp_path / "runs"), smoke_root=tmp_path / "smoke", e2e_root=e2e_root)
    server, base = _serve(handler)
    try:
        with urllib.request.urlopen(f"{base}/tokens") as r:
            return [run for run in json.load(r) if run["kind"] == "e2e"]
    finally:
        server.shutdown()
        server.server_close()


def test_e2e_run_that_died_without_marker_is_finished_with_stage_status(tmp_path: Path) -> None:
    """A run.sh run killed before writing any terminal marker is not shown as live."""
    run_dir = tmp_path / "e2e" / "e2e-010" / "results"
    run_dir.mkdir(parents=True)
    (run_dir / "context.json").write_text(
        json.dumps({"started_utc": "2026-09-29T08:30:00Z", "ca_timeout_s": 60, "discovery_timeout_s": 60})
    )
    (run_dir / "ca-result.json").write_text(json.dumps({"status": "failed"}))

    [run] = _list_tokens_e2e(tmp_path, tmp_path / "e2e")
    assert run["finished"] is True
    assert run["status"] == "failed"


def test_e2e_run_within_time_budget_without_marker_is_running(tmp_path: Path) -> None:
    from datetime import datetime, timezone

    run_dir = tmp_path / "e2e" / "e2e-011" / "results"
    run_dir.mkdir(parents=True)
    started = datetime.now(timezone.utc).isoformat()
    (run_dir / "context.json").write_text(
        json.dumps({"started_utc": started, "ca_timeout_s": 3600, "discovery_timeout_s": 3600})
    )

    [run] = _list_tokens_e2e(tmp_path, tmp_path / "e2e")
    assert run["finished"] is False


def test_sysinfo_reports_process_limits():
    from observability.wf_observer import sysinfo

    limits = sysinfo.collect()["process_limits"]
    assert {"cgroup_version", "rlimit_nproc_soft", "kernel_threads_max"} <= limits.keys()
