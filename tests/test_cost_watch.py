"""Tests for the hard spend cap (pipeline/cost_watch.py): the poll loop with injected cost and process functions,
then one real child process stopped at the cap."""

from __future__ import annotations

import json
import signal
import subprocess
import sys
from pathlib import Path

from pipeline import cost_watch as cw


class FakeProc:
    def __init__(self, dies_after_polls: int | None = None, ignores_term: bool = False):
        self.alive_checks = 0
        self.dies_after = dies_after_polls
        self.ignores_term = ignores_term
        self.signals: list[int] = []
        self.dead = False

    def alive(self, _pid: int) -> bool:
        self.alive_checks += 1
        if self.dies_after is not None and self.alive_checks > self.dies_after:
            self.dead = True
        return not self.dead

    def kill(self, _pid: int, sig: int) -> None:
        self.signals.append(sig)
        if sig == signal.SIGKILL or not self.ignores_term:
            self.dead = True


def costs(*values: float):
    it = iter(values)
    return lambda: {"est_usd": next(it), "unpriced_tokens": 0, "unpriced_models": [], "streams": ["s"]}


def _log(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def _watch(tmp_path, cost, proc, **kw):
    return cw.watch(
        cost,
        123,
        13.0,
        tmp_path / "w.jsonl",
        tmp_path / "hit.json",
        sleep=lambda _s: None,
        alive=proc.alive,
        kill=proc.kill,
        clock=lambda: "t",
        **kw,
    )


def test_below_the_cap_the_launcher_is_left_alone(tmp_path):
    proc = FakeProc(dies_after_polls=3)
    assert _watch(tmp_path, costs(1.0, 5.0, 9.0), proc) == 0
    assert proc.signals == [] and not (tmp_path / "hit.json").exists()
    assert [e["est_usd"] for e in _log(tmp_path / "w.jsonl")] == [1.0, 5.0, 9.0]


def test_at_the_cap_the_launcher_is_stopped_and_the_hit_recorded(tmp_path):
    proc = FakeProc()
    assert _watch(tmp_path, costs(4.0, 13.2), proc) == cw.EXIT_CAP_HIT
    assert proc.signals == [signal.SIGTERM]
    hit = json.loads((tmp_path / "hit.json").read_text())
    assert hit["event"] == "cap_hit" and hit["est_usd"] == 13.2 and hit["cap_usd"] == 13.0


def test_a_launcher_that_ignores_sigterm_gets_sigkill(tmp_path):
    proc = FakeProc(ignores_term=True)
    assert _watch(tmp_path, costs(20.0), proc, grace=3) == cw.EXIT_CAP_HIT
    assert proc.signals == [signal.SIGTERM, signal.SIGKILL]
    assert _log(tmp_path / "w.jsonl")[-1]["event"] == "sigkill"


def test_a_failed_estimate_is_logged_and_never_stops_the_run(tmp_path):
    def broken():
        raise RuntimeError("transcripts not there yet")

    proc = FakeProc(dies_after_polls=2)
    assert _watch(tmp_path, broken, proc) == 0
    assert proc.signals == [] and _log(tmp_path / "w.jsonl")[0]["event"] == "estimate_failed"


def test_current_cost_of_a_run_without_streams_is_zero(tmp_path):
    state = cw.current_cost(tmp_path, {"limits": {}, "prices_usd_per_mtok": {}})
    assert state["est_usd"] == 0.0 and state["streams"] == []


def test_real_process_is_stopped(tmp_path, monkeypatch):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        code = cw.watch(
            costs(50.0), child.pid, 13.0, tmp_path / "w.jsonl", tmp_path / "hit.json", interval=0.1, grace=5
        )
        assert code == cw.EXIT_CAP_HIT
        assert child.wait(timeout=10) == -signal.SIGTERM
    finally:
        if child.poll() is None:
            child.kill()


def test_cli_rejects_a_non_positive_cap(tmp_path):
    assert cw.main([str(tmp_path), "--pid", "1", "--cap-usd", "0"]) == 2
