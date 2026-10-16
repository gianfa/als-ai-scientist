"""run_stages / GET /runs/<id>/stages, exercised against copies of REAL results directories."""

import json
import shutil
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from observability.wf_observer.server import make_handler, run_stages
from observability.wf_observer.store import RunStore

REAL_EXPERIMENTS = Path(__file__).resolve().parents[1] / "experiments"
# Only the small files that carry stage state; journals/outputs are irrelevant to run_stages.
STAGE_FILES = (
    "ca-result.json",
    "discovery-result.json",
    "failed_stage",
    "run_exit_code",
    "exit_code",
    "summary.json",
    "context.json",
)


def _copy_real(run_id: str, dest_root: Path) -> Path:
    src = REAL_EXPERIMENTS / run_id / "results"
    if not src.is_dir():
        pytest.skip(f"real run {run_id} not present")
    dest = dest_root / run_id / "results"
    dest.mkdir(parents=True)
    for name in STAGE_FILES:
        if (src / name).is_file():
            shutil.copy(src / name, dest / name)
    if (src / "evaluation" / "evaluation.json").is_file():
        (dest / "evaluation").mkdir()
        shutil.copy(src / "evaluation" / "evaluation.json", dest / "evaluation" / "evaluation.json")
    return dest


def _by_stage(results_dir: Path) -> dict[str, str]:
    return {s.stage: s.status for s in run_stages(results_dir)}


def test_stage_order_is_fixed(tmp_path: Path) -> None:
    assert [s.stage for s in run_stages(_copy_real("e2e-011", tmp_path))] == ["ca", "discovery", "evaluation"]


def test_failed_at_discovery_real_e2e_011(tmp_path: Path) -> None:
    results = _copy_real("e2e-011", tmp_path)
    assert _by_stage(results) == {"ca": "passed", "discovery": "failed", "evaluation": "not_started"}
    discovery = run_stages(results)[1]
    assert discovery.detail and discovery.detail.startswith("stopped")


def test_fully_successful_run(tmp_path: Path) -> None:
    # No fully-green run exists on disk (no e2e run has completed discovery), so compose one from real
    # files: a real completed CA record and a real completed workflow record, plus a real evaluation.json.
    ca = REAL_EXPERIMENTS / "e2e-011" / "results" / "ca-result.json"
    evaluations = sorted(REAL_EXPERIMENTS.glob("e2e-*/results/evaluation/evaluation.json"))
    if not ca.is_file() or not evaluations:
        pytest.skip("real artifacts not present")
    results = tmp_path / "e2e-999" / "results"
    (results / "evaluation").mkdir(parents=True)
    shutil.copy(ca, results / "ca-result.json")
    record = json.loads(ca.read_text())  # same real shape, status "completed"
    (results / "discovery-result.json").write_text(json.dumps(record))
    shutil.copy(evaluations[0], results / "evaluation" / "evaluation.json")
    (results / "run_exit_code").write_text("0\n")
    assert _by_stage(results) == {"ca": "passed", "discovery": "passed", "evaluation": "passed"}


def test_ca_failed_without_discovery_real_e2e_010(tmp_path: Path) -> None:
    results = _copy_real("e2e-010", tmp_path)
    (results / "run_exit_code").write_text("1\n")
    states = _by_stage(results)
    assert states["ca"] == "failed" and states["discovery"] == "not_started"


def test_failed_stage_marker_without_record(tmp_path: Path) -> None:
    results = _copy_real("e2e-011", tmp_path)
    (results / "discovery-result.json").unlink()
    assert _by_stage(results)["discovery"] == "failed"  # failed_stage=discovery from the real run


def test_missing_artifacts(tmp_path: Path) -> None:
    results = tmp_path / "e2e-998" / "results"
    results.mkdir(parents=True)
    (results / "run_exit_code").write_text("1\n")  # finished, nothing recorded
    assert _by_stage(results) == {"ca": "not_started", "discovery": "not_started", "evaluation": "not_started"}


def test_live_run_marks_first_pending_stage_running(tmp_path: Path) -> None:
    results = _copy_real("e2e-011", tmp_path)
    for name in ("discovery-result.json", "failed_stage", "run_exit_code", "exit_code", "summary.json"):
        (results / name).unlink(missing_ok=True)
    context = json.loads((results / "context.json").read_text())
    context["started_utc"] = datetime.now(timezone.utc).isoformat()  # real context, run just started
    (results / "context.json").write_text(json.dumps(context))
    assert _by_stage(results) == {"ca": "passed", "discovery": "running", "evaluation": "not_started"}


def test_stages_endpoint(tmp_path: Path) -> None:
    root = tmp_path / "experiments"
    _copy_real("e2e-011", root)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(RunStore(tmp_path / "store"), experiments_root=root))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with urllib.request.urlopen(f"{base}/runs/e2e-011/stages") as resp:
            body = json.load(resp)
        assert [(s["stage"], s["status"]) for s in body] == [
            ("ca", "passed"),
            ("discovery", "failed"),
            ("evaluation", "not_started"),
        ]
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(f"{base}/runs/e2e-777/stages")
        assert exc.value.code == 404
    finally:
        server.shutdown()
        server.server_close()
