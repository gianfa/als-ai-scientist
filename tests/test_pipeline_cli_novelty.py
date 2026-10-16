"""`python -m pipeline.cli novelty` refuses what it must refuse before any paid session."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline import cli


@pytest.fixture
def runs(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    return tmp_path / "experiments"


def _run_dir(runs: Path, run_id: str, completed: list[str]) -> Path:
    results = runs / run_id / "results"
    results.mkdir(parents=True)
    (results / "state.json").write_text(json.dumps({"run_id": run_id, "completed": completed, "phases": {}}))
    (results / "context.json").write_text(json.dumps({"kb": str(runs / run_id / "kb")}))
    return results


def test_novelty_needs_an_existing_run(runs, capsys):
    assert cli.main(["novelty", "--run-id", "nope"]) == 2
    assert "no run nope" in capsys.readouterr().err


def test_novelty_refuses_a_run_that_has_not_finished(runs, capsys):
    _run_dir(runs, "r1", ["data", "literature"])
    assert cli.main(["novelty", "--run-id", "r1"]) == 2
    assert "synthesize" in capsys.readouterr().err


def test_novelty_is_a_verb_and_not_a_phase_of_run():
    assert "novelty" in cli.VERBS and "novelty" not in cli.PHASES


# --- `run` goes on to the novelty stage unless told not to ----------------------------------------------------


class _FakeOrchestrator:
    """Stands in for the paid pipeline: `run` returns a canned state, `run_novelty` records that it was asked."""

    completed: list[str] = []
    novelty_calls = 0
    novelty_error: Exception | None = None

    def __init__(self, *args, **kwargs):
        pass

    def run(self, phases):
        return {"completed": list(type(self).completed)}

    def archive_novelty_attempt(self):
        return None

    def run_novelty(self):
        type(self).novelty_calls += 1
        if type(self).novelty_error is not None:
            raise type(self).novelty_error
        return {"status": "assessed", "assessed_count": 0, "entries": []}


@pytest.fixture
def fake_run(runs, monkeypatch):
    class Fake(_FakeOrchestrator):
        completed = ["data", "synthesize"]
        novelty_calls = 0
        novelty_error = None

    monkeypatch.setattr(cli, "Orchestrator", Fake)
    monkeypatch.setattr(cli, "ClaudeLauncher", lambda *a, **k: None)
    monkeypatch.setattr(cli, "load_config", lambda path: type("C", (), {"config_hash": "h"})())
    monkeypatch.setattr(cli, "write_pid_file", lambda *a, **k: None)
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: type("R", (), {"stdout": "abc"})())
    monkeypatch.setattr("evaluation.novelty_diagnosis.write", lambda run_dir, cache: {"problems": 0}, raising=False)
    return Fake


def test_run_assesses_novelty_after_synthesize(fake_run, capsys):
    assert cli.main(["run", "--run-id", "r1"]) == 0
    assert fake_run.novelty_calls == 1


def test_no_novelty_leaves_the_stage_out(fake_run):
    assert cli.main(["run", "--run-id", "r1", "--no-novelty"]) == 0
    assert fake_run.novelty_calls == 0


def test_a_run_that_did_not_reach_synthesize_launches_no_novelty(fake_run):
    fake_run.completed = ["data", "compute"]
    assert cli.main(["run", "--run-id", "r1"]) == 0
    assert fake_run.novelty_calls == 0


def test_a_novelty_stage_the_cap_cannot_pay_for_does_not_fail_a_finished_run(fake_run, capsys):
    fake_run.novelty_error = cli.PhaseFailed("novelty: only 0.09 USD left of the run cap")
    assert cli.main(["run", "--run-id", "r1"]) == 0
    err = capsys.readouterr().err
    assert "novelty was not assessed" in err and "pipeline.cli novelty --id r1" in err
