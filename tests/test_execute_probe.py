"""`execute` in isolation, on the delivery cases of `tests/fixtures/execute/`.

Every case names a minimal knowledge base (already holding a bought `Decision` and a matching `Experiment`) and
the local sources that `Experiment` declares, so nothing here reads an archived run or the shared store. The
mocked tests run always; `test_a_live_executor_runs_each_case` launches a real agent per case and runs only with
`ALS_LIVE_EXECUTE=1` (paid) — and, unlike `reassess`'s live test, that agent makes a real tool call that runs the
real registered handler on the case's own synthetic data.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
from pathlib import Path

import pytest

from pipeline.config import DEFAULT_CONFIG
from pipeline.execute_probe import (
    DEFAULT_MAX_COST_USD,
    InvalidDelivery,
    check_selection,
    load_case,
    prepare,
    probe_config,
    run,
)
from pipeline.launcher import FakeLauncher
from tools.data.local_sources import LocalSource, manifest
from tools.experiment.mcp_server import run_experiment

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).parent / "fixtures" / "execute"
CASES = sorted((FIXTURE / "cases").glob("*.json"))
LIVE = os.environ.get("ALS_LIVE_EXECUTE") == "1"


def now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def digests(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def test_the_fixture_holds_the_ics_case():
    assert {c.stem for c in CASES} >= {"ics_cytokine_skew_v1"}


@pytest.mark.parametrize("path", CASES, ids=lambda p: p.stem)
def test_every_case_is_selectable_as_written(path):
    case = load_case(path)
    manifest_data = manifest(case.sources, ROOT)
    assert check_selection(case, manifest_data) == []


def test_a_decision_naming_no_such_experiment_is_refused(tmp_path):
    case = load_case(FIXTURE / "cases" / "ics_cytokine_skew_v1.json")
    manifest_data = manifest(case.sources, ROOT)
    broken = type(case)(case.name, case.run_id, case.kb, "EXP-404", case.sources)
    assert check_selection(broken, manifest_data) == ["no 'buy' decision of run FIX-EXECUTE names EXP-404"]


def test_a_run_id_with_no_matching_decision_is_refused():
    case = load_case(FIXTURE / "cases" / "ics_cytokine_skew_v1.json")
    manifest_data = manifest(case.sources, ROOT)
    other = type(case)(case.name, "RUN-2", case.kb, case.experiment_id, case.sources)
    assert check_selection(other, manifest_data) == ["no 'buy' decision of run RUN-2 names EXP-1"]


def test_a_missing_source_is_refused_before_anything_is_written(tmp_path):
    case = load_case(FIXTURE / "cases" / "ics_cytokine_skew_v1.json")
    missing = LocalSource(
        id=case.sources[0].id, path=tmp_path / "absent", kind=case.sources[0].kind, license_note="fixture case"
    )
    broken = type(case)(case.name, case.run_id, case.kb, case.experiment_id, (missing, case.sources[1]))
    with pytest.raises(InvalidDelivery):
        prepare(broken, tmp_path, now())
    assert not list(tmp_path.iterdir())


def fake_executor(run_dir: Path):
    """Stands in for the live agents: one per selected EXP, each calling the real MCP tool as its prompt says."""

    def execute_tool(args):
        outputs = []
        for selected in args["selected"]:
            result = run_experiment(run_dir / "execution-policy.json", selected["experiment_id"])
            assert "error" not in result, result
            outputs.append(
                {
                    "agent_id": f"executor@{selected['experiment_id']}",
                    "output": {
                        "experiment_id": result["experiment_id"],
                        "analysis_id": result["analysis_id"],
                        "result_digest": result["result_digest"],
                        "outcome": result["outcome"],
                        "interpretation": "supports_ifng_skew_only",
                        "limitations": result["limitations"],
                    },
                }
            )
        return {"outputs": outputs}

    return execute_tool


@pytest.mark.parametrize("path", CASES, ids=lambda p: p.stem)
def test_each_case_is_executed_in_its_own_directory_and_the_fixture_is_untouched(tmp_path, path):
    case = load_case(path)
    before = digests(FIXTURE)
    probe = prepare(case, tmp_path, now())
    report = run(
        probe,
        probe_config(DEFAULT_CONFIG, DEFAULT_MAX_COST_USD),
        launcher_factory=lambda config, kb_root: FakeLauncher({"execute": fake_executor(probe.run_dir)}),
    )

    assert report["status"] == "executed", report.get("reason")
    row = next(r for r in report["executions"] if r["experiment_id"] == case.experiment_id)
    assert row["status"] == "executed", row.get("reason")
    assert row["interpretation_check"]["faithful"], row["interpretation_check"]["errors"]
    assert digests(FIXTURE) == before
    assert (probe.run_dir / "execution-policy.json").is_file()
    assert (probe.run_dir / "experiment-analysis" / case.experiment_id / "summary.json").is_file()
    assert json.loads((probe.run_dir / "probe.json").read_text())["run_max_cost_usd"] == DEFAULT_MAX_COST_USD


def test_an_invalid_case_writes_nothing(tmp_path):
    case = load_case(FIXTURE / "cases" / "ics_cytokine_skew_v1.json")
    broken = type(case)(case.name, case.run_id, case.kb, "EXP-404", case.sources)
    with pytest.raises(InvalidDelivery, match="EXP-404"):
        prepare(broken, tmp_path, now())
    assert not list(tmp_path.iterdir())


def test_a_probe_never_overwrites_another(tmp_path):
    case = load_case(FIXTURE / "cases" / "ics_cytokine_skew_v1.json")
    at = now()
    prepare(case, tmp_path, at)
    with pytest.raises(FileExistsError):
        prepare(case, tmp_path, at)


@pytest.mark.skipif(not LIVE, reason="paid: set ALS_LIVE_EXECUTE=1 to launch a live executor per case")
@pytest.mark.parametrize("path", CASES, ids=lambda p: p.stem)
def test_a_live_executor_runs_each_case(tmp_path, path):
    """The agent, its prompt and the real registered handler, on an input known to be selectable: a failure
    here is the executor's or the handler's, not the delivery's. The probe's archive stays under `tmp_path`."""
    case = load_case(path)
    probe = prepare(case, tmp_path, now())
    report = run(probe, probe_config(DEFAULT_CONFIG, DEFAULT_MAX_COST_USD))
    check = report.get("interpretation_check") or {}
    print(
        json.dumps(
            {
                "case": case.name,
                "status": report.get("status"),
                "faithful": check.get("faithful"),
                "errors": check.get("errors"),
                "results": str(probe.run_dir),
                "cost_usd": (report.get("launch") or {}).get("session_cost_usd"),
            },
            indent=2,
        )
    )
    assert report["status"] == "executed", report.get("reason")
    assert check.get("faithful"), check.get("errors")
