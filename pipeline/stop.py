"""Stopping a run, and the records that make it possible.

A live run is paid, so it must be stoppable by one command — and the command must not look for it in the
process table. `pgrep -f "pipeline.cli --run-id <id>"` matches the shell running that very `pgrep`,
because the pattern is in its own command line, so a finished run reads as alive and a wait loop never exits.
Conversely, killing the orchestrator's process group can leave the phase's paid session running, orphaned with
PPID 1 and with no `cost_watch`, i.e. spending with no cap.

So a run writes down who it is. The orchestrator records itself in `run.pid` and each phase records its paid
session in `phase.pid`, both inside the run's results directory, and `stop_run` reads those files. Nothing
here searches for a process by name.

A pid outlives its process and the number is reused, so a recorded pid is signalled only when the process
still **identifies itself as this run**: its `/proc/<pid>/cmdline` must mention the run id (the orchestrator
and the session carry `--run-id`) or the run's own directory (the cost watcher is launched with it). A pid
that is gone, or that now belongs to something else, is reported and never signalled.
"""

from __future__ import annotations

import json
import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

RUN_PID_FILE = "run.pid"
PHASE_PID_FILE = "phase.pid"
GRACE_S = 10.0  # between SIGTERM and SIGKILL: long enough for a session to flush its stream


@dataclass(frozen=True)
class Recorded:
    """One process a run wrote down: what it is, and what it claimed to be."""

    role: str  # "orchestrator", "session", "cost_watch"
    pid: int
    pgid: int | None = None


def write_pid_file(run_dir: Path, name: str, role: str, pid: int, pgid: int | None, extra: dict | None = None) -> Path:
    """Record one process of this run. Overwrites: there is one orchestrator and one live phase at a time."""
    path = run_dir / name
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {"role": role, "pid": pid, "pgid": pgid, "written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    path.write_text(json.dumps(body | (extra or {}), indent=2) + "\n", encoding="utf-8")
    return path


def clear_pid_file(run_dir: Path, name: str) -> None:
    """Remove a record on a clean exit. A record left behind is a crash, and `stop` says so."""
    (run_dir / name).unlink(missing_ok=True)


def recorded_processes(run_dir: Path) -> list[Recorded]:
    """Every process this run wrote down, orchestrator first. Unreadable records are skipped, not raised."""
    out: list[Recorded] = []
    for name in (RUN_PID_FILE, PHASE_PID_FILE):
        path = run_dir / name
        if not path.is_file():
            continue
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
            pid = int(body["pid"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue
        pgid = body.get("pgid")
        out.append(
            Recorded(role=str(body.get("role") or name), pid=pid, pgid=int(pgid) if isinstance(pgid, int) else None)
        )
    return out


def _cmdline(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf-8", "replace").replace("\0", " ")
    except OSError:
        return ""


def default_identity(run_id: str, run_dir: Path) -> Callable[[int], bool]:
    """True when the live process with this pid still belongs to this run.

    Reads `/proc/<pid>/cmdline` for one named pid. This is not a search of the process table: nothing is
    matched by pattern across processes, so the `pgrep` trap cannot apply.
    """
    needles = (f"--run-id {run_id}", f"--run-id={run_id}", str(run_dir))

    def identifies(pid: int) -> bool:
        line = _cmdline(pid)
        return bool(line) and any(n in line for n in needles)

    return identifies


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def stop_run(
    run_dir: Path,
    run_id: str,
    *,
    identifies: Callable[[int], bool] | None = None,
    alive: Callable[[int], bool] = _alive,
    kill: Callable[[int, int], None] = os.kill,
    killpg: Callable[[int, int], None] = os.killpg,
    sleep: Callable[[float], None] = time.sleep,
    grace_s: float = GRACE_S,
) -> dict:
    """Stop every process this run recorded: `SIGTERM`, a grace period, then `SIGKILL` on what survives.

    The process group is signalled when one was recorded, because a phase's paid session and its cost watcher
    are children of the orchestrator and a group signal reaches them in one go; the individual pids are then
    signalled too, which is what catches a session already orphaned from the group.

    Pure except for the injected system calls, so the whole decision table is testable without a victim.
    """
    identifies = identifies or default_identity(run_id, run_dir)
    records = recorded_processes(run_dir)
    report: dict = {
        "run_id": run_id,
        "recorded": len(records),
        "signalled": [],
        "already_gone": [],
        "not_this_run": [],
        "killed": [],
        "survived": [],
    }
    if not records:
        return report

    targets: list[Recorded] = []
    for record in records:
        if not alive(record.pid):
            report["already_gone"].append({"role": record.role, "pid": record.pid})
            continue
        if not identifies(record.pid):
            report["not_this_run"].append({"role": record.role, "pid": record.pid})
            continue
        targets.append(record)

    groups = {r.pgid for r in targets if r.pgid}
    for pgid in sorted(groups):
        try:
            killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError) as err:
            report.setdefault("group_errors", []).append({"pgid": pgid, "error": type(err).__name__})
    for record in targets:
        try:
            kill(record.pid, signal.SIGTERM)
            report["signalled"].append({"role": record.role, "pid": record.pid, "signal": "SIGTERM"})
        except ProcessLookupError:
            report["already_gone"].append({"role": record.role, "pid": record.pid})

    if not targets:
        return report
    sleep(grace_s)
    for record in targets:
        if not alive(record.pid):
            continue
        try:
            kill(record.pid, signal.SIGKILL)
            report["killed"].append({"role": record.role, "pid": record.pid})
        except ProcessLookupError:
            continue
    for record in targets:
        if alive(record.pid):
            report["survived"].append({"role": record.role, "pid": record.pid})
    return report


def completed_phases(run_dir: Path) -> list[str]:
    """What the run's own state says it finished — the only honest answer when no process is left."""
    path = run_dir / "state.json"
    if not path.is_file():
        return []
    try:
        return list(json.loads(path.read_text(encoding="utf-8")).get("completed") or [])
    except (json.JSONDecodeError, AttributeError):
        return []
