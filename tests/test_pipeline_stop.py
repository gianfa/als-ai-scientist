"""Tests for pipeline.stop and the CLI's verbs: a run is stopped from what it wrote down.

Every system call is injected, so the decision table is exercised without a victim process and without ever
reading the process table — which is the fault these tests exist to prevent.
"""

from __future__ import annotations

import json
import signal
from pathlib import Path

import pytest

from pipeline import cli
from pipeline import stop as st


def _record(run_dir: Path, name: str, role: str, pid: int, pgid: int | None = None) -> None:
    st.write_pid_file(run_dir, name, role, pid, pgid)


class Fake:
    """A stand-in for the signals: records what was sent and lets a pid die on cue."""

    def __init__(self, alive: set[int], dies_on_term: set[int] | None = None) -> None:
        self.living = set(alive)
        self.dies_on_term = set(dies_on_term or ())
        self.sent: list[tuple[str, int, int]] = []
        self.slept: list[float] = []

    def alive(self, pid: int) -> bool:
        return pid in self.living

    def kill(self, pid: int, sig: int) -> None:
        if pid not in self.living:
            raise ProcessLookupError(pid)
        self.sent.append(("kill", pid, sig))
        if sig == signal.SIGTERM and pid in self.dies_on_term:
            self.living.discard(pid)
        if sig == signal.SIGKILL:
            self.living.discard(pid)

    def killpg(self, pgid: int, sig: int) -> None:
        self.sent.append(("killpg", pgid, sig))

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)


def test_a_pid_file_is_written_and_cleared(tmp_path: Path) -> None:
    path = st.write_pid_file(tmp_path, st.RUN_PID_FILE, "orchestrator", 4242, 99, extra={"run_id": "pl-x"})
    body = json.loads(path.read_text())
    assert body["pid"] == 4242 and body["pgid"] == 99 and body["role"] == "orchestrator"
    assert body["run_id"] == "pl-x" and body["written_at"].endswith("Z")
    st.clear_pid_file(tmp_path, st.RUN_PID_FILE)
    assert not path.exists()
    st.clear_pid_file(tmp_path, st.RUN_PID_FILE)  # idempotent: a clean exit may race a stop


def test_records_are_read_orchestrator_first_and_rubbish_is_skipped(tmp_path: Path) -> None:
    _record(tmp_path, st.RUN_PID_FILE, "orchestrator", 10, 10)
    (tmp_path / st.PHASE_PID_FILE).write_text("not json at all")
    assert [r.role for r in st.recorded_processes(tmp_path)] == ["orchestrator"]
    _record(tmp_path, st.PHASE_PID_FILE, "session", 11)
    assert [(r.role, r.pid, r.pgid) for r in st.recorded_processes(tmp_path)] == [
        ("orchestrator", 10, 10),
        ("session", 11, None),
    ]


def test_stopping_a_run_that_recorded_nothing_signals_nothing(tmp_path: Path) -> None:
    fake = Fake(alive={1})
    report = st.stop_run(tmp_path, "pl-x", alive=fake.alive, kill=fake.kill, killpg=fake.killpg, sleep=fake.sleep)
    assert report["recorded"] == 0 and fake.sent == [] and fake.slept == []


def test_a_recorded_pid_that_is_gone_is_reported_not_signalled(tmp_path: Path) -> None:
    _record(tmp_path, st.RUN_PID_FILE, "orchestrator", 777, 777)
    fake = Fake(alive=set())
    report = st.stop_run(
        tmp_path,
        "pl-x",
        identifies=lambda pid: True,
        alive=fake.alive,
        kill=fake.kill,
        killpg=fake.killpg,
        sleep=fake.sleep,
    )
    assert report["already_gone"] == [{"role": "orchestrator", "pid": 777}]
    assert fake.sent == [] and fake.slept == []  # nothing is signalled, and no grace is waited


def test_a_recycled_pid_is_never_signalled(tmp_path: Path) -> None:
    """The pid is alive but now belongs to something else: killing it would kill a stranger."""
    _record(tmp_path, st.RUN_PID_FILE, "orchestrator", 555, 555)
    fake = Fake(alive={555})
    report = st.stop_run(
        tmp_path,
        "pl-x",
        identifies=lambda pid: False,
        alive=fake.alive,
        kill=fake.kill,
        killpg=fake.killpg,
        sleep=fake.sleep,
    )
    assert report["not_this_run"] == [{"role": "orchestrator", "pid": 555}]
    assert fake.sent == []


def test_the_group_and_each_pid_get_sigterm_then_sigkill_on_survivors(tmp_path: Path) -> None:
    _record(tmp_path, st.RUN_PID_FILE, "orchestrator", 100, 100)
    _record(tmp_path, st.PHASE_PID_FILE, "session", 101)  # orphaned: no group of its own on record
    fake = Fake(alive={100, 101})
    report = st.stop_run(
        tmp_path,
        "pl-x",
        identifies=lambda pid: True,
        alive=fake.alive,
        kill=fake.kill,
        killpg=fake.killpg,
        sleep=fake.sleep,
        grace_s=3.0,
    )
    assert ("killpg", 100, signal.SIGTERM) in fake.sent
    assert ("kill", 100, signal.SIGTERM) in fake.sent and ("kill", 101, signal.SIGTERM) in fake.sent
    assert fake.slept == [3.0]
    assert sorted(k["pid"] for k in report["killed"]) == [100, 101]
    assert report["survived"] == [] and not fake.living


def test_a_process_that_goes_on_sigterm_is_not_killed(tmp_path: Path) -> None:
    _record(tmp_path, st.RUN_PID_FILE, "orchestrator", 200, 200)
    fake = Fake(alive={200}, dies_on_term={200})
    report = st.stop_run(
        tmp_path,
        "pl-x",
        identifies=lambda pid: True,
        alive=fake.alive,
        kill=fake.kill,
        killpg=fake.killpg,
        sleep=fake.sleep,
    )
    assert [s for s in fake.sent if s[0] == "kill"] == [("kill", 200, signal.SIGTERM)]
    assert report["killed"] == [] and report["survived"] == []


@pytest.mark.parametrize(
    "cmdline, expected",
    [
        ("python -m pipeline.cli run --run-id pl-prod-009 --config configs/pipeline.yaml", True),
        ("python -m pipeline.cli run --run-id=pl-prod-009", True),
        ("uv run python pipeline/cost_watch.py RUNDIR --pid 12 --cap-usd 24", True),  # the watcher names the dir
        ("python -m pipeline.cli run --run-id pl-prod-010", False),  # a different run
        ("", False),  # /proc gone: never signal
        ("/usr/bin/some-unrelated-daemon --serve", False),
    ],
)
def test_identity_matches_only_a_process_that_names_this_run(tmp_path, monkeypatch, cmdline, expected) -> None:
    monkeypatch.setattr(st, "_cmdline", lambda pid: cmdline.replace("RUNDIR", str(tmp_path)))
    assert st.default_identity("pl-prod-009", tmp_path)(4242) is expected


def test_identity_is_consulted_before_any_signal(tmp_path: Path, monkeypatch) -> None:
    """The guard is wired into `stop_run`'s default, not only available next to it."""
    _record(tmp_path, st.RUN_PID_FILE, "orchestrator", 4242, 4242)
    monkeypatch.setattr(st, "_cmdline", lambda pid: "/usr/bin/some-unrelated-daemon")
    fake = Fake(alive={4242})
    report = st.stop_run(tmp_path, "pl-x", alive=fake.alive, kill=fake.kill, killpg=fake.killpg, sleep=fake.sleep)
    assert report["not_this_run"] and fake.sent == []


def test_completed_phases_reads_the_state_and_tolerates_its_absence(tmp_path: Path) -> None:
    assert st.completed_phases(tmp_path) == []
    (tmp_path / "state.json").write_text(json.dumps({"completed": ["data", "compute"]}))
    assert st.completed_phases(tmp_path) == ["data", "compute"]
    (tmp_path / "state.json").write_text("{broken")
    assert st.completed_phases(tmp_path) == []


# ---- the CLI's verbs ---------------------------------------------------------------------------------


def test_a_bare_run_id_still_means_run() -> None:
    assert cli._normalise(["--run-id", "pl-x"]) == ["run", "--run-id", "pl-x"]
    assert cli._normalise(["run", "--id", "pl-x"]) == ["run", "--id", "pl-x"]
    assert cli._normalise(["stop", "--id", "pl-x"]) == ["stop", "--id", "pl-x"]
    assert cli._normalise(["--help"]) == ["--help"]


def test_stop_of_an_unknown_run_says_so(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["stop", "--id", "pl-does-not-exist-anywhere"]) == 2
    assert "no run pl-does-not-exist-anywhere" in capsys.readouterr().err


def test_stop_with_no_record_reports_the_archive_instead_of_guessing(tmp_path: Path, monkeypatch, capsys) -> None:
    run_dir = tmp_path / "experiments" / "pl-y" / "results"
    run_dir.mkdir(parents=True)
    (run_dir / "state.json").write_text(json.dumps({"completed": ["data", "compute", "literature"]}))
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    assert cli.main(["stop", "--id", "pl-y"]) == 1
    err = capsys.readouterr().err
    assert "recorded no live process" in err and "3 phase(s) completed" in err and "literature" in err


def test_the_cli_refuses_an_invalid_run_id() -> None:
    with pytest.raises(SystemExit):
        cli.main(["stop", "--id", "../etc/passwd"])
