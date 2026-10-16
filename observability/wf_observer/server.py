"""Read-only local UI and JSON API over the run store.

GET /                          -> UI (run views: Overview, Workflow, Phase graph, Agents, Story, Tokens, Evaluation)
GET /runs                      -> list of run IDs
GET /runs/running              -> list of currently running runs
GET /runs/<run_id>             -> context.json
GET /runs/<run_id>/events      -> events
GET /runs/<run_id>/story       -> plain-language narrative + agent exchanges (with tool calls on a phase-pipeline run)
GET /runs/<run_id>/stages      -> ordered per-phase status of a run (per stage for the previous run.sh runs)
GET /runs/<run_id>/metrics     -> computed metrics (from the phase archive for a phase-pipeline run)
GET /runs/<run_id>/phases      -> the executed phase chain of a phase-pipeline run: status, attempts, agents, cost
GET /runs/<run_id>/roles       -> per phase of a phase-pipeline run: role file, launch settings, agents started
GET /tokens                    -> list of smoke runs with token status
GET /tokens/<run>?mode=live|saved -> token data for a run
GET /tokens-config             -> {poll_s: int} for UI polling interval
GET /evaluation/<run_id>       -> the scorecard of a run (evaluation.json for the previous run.sh runs)
GET /static/<file>             -> static assets: the UI's ES modules under js/ and chart.umd.min.js
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from time import monotonic
from typing import Any, Literal
from urllib.parse import parse_qs, urlparse

from pipeline.stop import default_identity, recorded_processes

from ._config import EXPERIMENTS_ROOT, PROJECT_ROOT, SMOKE_ROOT
from .catalog import RunCatalog, is_safe_name
from .journal import workflow_journal_events
from .metrics import summarize
from .phase_reports import has_phase_reports, phase_usages, run_totals, tokens_view
from .pipeline_run import pipeline_metrics, run_phases, run_roles
from .store import RunNotFoundError, RunStore
from .story import run_story
from .tokens import aggregate, collect_usage, load_tokens_config

UI_FILE = Path(__file__).with_name("ui.html")
STATIC_DIR = Path(__file__).parent / "static"
# Run id validation lives in the catalog, so every endpoint accepts the ids `GET /runs` lists.


# Files whose presence means run.sh reached a terminal state. Older runs predate
# `exit_code` and only leave result.json / summary.txt behind. run_exit_code is set by
# run.sh's EXIT trap and serves as a primary terminal marker.
_TERMINAL_MARKERS = ("exit_code", "run_exit_code", "result.json", "summary.json", "summary.txt")
# Slack added to the configured workflow timeouts before a marker-less run is
# considered dead (killed launcher, crashed shell, closed terminal).
_RUNNING_GRACE_S = 600

# Response caching for GET /runs/running
_RUNNING_MAX_RESULTS = 20
_RUNNING_CACHE_TTL_S = 5.0


def _tail_jsonl(path: Path, max_bytes: int = 64 * 1024) -> list[dict]:
    """Read only the tail of a JSONL file, up to max_bytes.

    Seeks to max(0, size - max_bytes), drops the first partial line if it seeked,
    parses remaining lines as JSON, skips malformed lines, returns dicts only.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return []

    result = []

    try:
        with path.open(encoding="utf-8") as f:
            # Seek to tail if file is larger than max_bytes
            if size > max_bytes:
                f.seek(size - max_bytes)
                # Drop the first partial line
                f.readline()

            # Parse remaining complete lines
            for line in f:
                line = line.strip()
                if line:
                    try:
                        obj = json.loads(line)
                        if isinstance(obj, dict):
                            result.append(obj)
                    except json.JSONDecodeError:
                        # Skip malformed lines
                        pass
    except OSError:
        pass

    return result


def _extract_phase_from_journals(results_dir: Path) -> dict[str, Any] | None:
    """Extract current phase and last event from journal files.

    Tries discovery-journal.jsonl, ca-journal.jsonl, or journal.jsonl.
    Returns dict with: stage, last_event_time, last_event, percent_complete.
    Uses tail-read only for efficiency.
    """
    # Phase heuristics for percent_complete
    phase_completeness = {
        "Evidence": 10,
        "EvidenceCheck": 20,
        "ComputationalAnalysis": 30,
        "Hypotheses": 40,
        "Critique": 50,
        "Revision": 60,
        "Intervention": 70,
        "ExperimentDesigner": 80,
        "Synthesis": 90,
        "Evaluation": 95,
        "Execute": 30,  # CA execution phase
        "Verify": 40,  # CA verification phase
        "Done": 100,
    }

    journal_files = [
        results_dir / "discovery-journal.jsonl",
        results_dir / "ca-journal.jsonl",
        results_dir / "journal.jsonl",
    ]

    last_event = None
    last_phase = "unknown"

    for journal_file in journal_files:
        if not journal_file.is_file():
            continue

        # Read only the tail of each journal file
        events = _tail_jsonl(journal_file)

        # Find the last event with "type" and track the last "phase"
        for event in events:
            if event.get("type"):
                last_event = event
            if event.get("phase"):
                last_phase = event.get("phase")

    if not last_event:
        return None

    # Format last event time
    if isinstance(last_event, dict):
        event_time = last_event.get("timestamp", "")
        event_type = last_event.get("type", "unknown")
        event_label = last_event.get("label", event_type)

        result = {
            "stage": last_phase,
            "last_event_time": event_time,
            "last_event": event_label,
            "percent_complete": phase_completeness.get(last_phase, 0),
        }
        return result

    return None


def _extract_metrics_from_outputs(results_dir: Path) -> dict[str, Any] | None:
    """Extract cost and token metrics from output files.

    Looks for task_progress events in discovery-output.jsonl or ca-output.jsonl.
    Returns dict with: cost_usd, tokens.
    Uses tail-read only for efficiency.
    """
    output_files = [
        results_dir / "discovery-output.jsonl",
        results_dir / "ca-output.jsonl",
    ]

    total_tokens = 0

    for output_file in output_files:
        if not output_file.is_file():
            continue

        # Read only the tail of each output file
        events = _tail_jsonl(output_file)

        for event in events:
            # Look for task_progress events with usage information
            if event.get("type") == "system" and event.get("subtype") == "task_progress":
                usage = event.get("usage", {})
                if usage.get("total_tokens"):
                    total_tokens = max(total_tokens, usage.get("total_tokens", 0))

    # Return metrics
    if total_tokens > 0:
        return {
            "tokens": total_tokens,
            "cost_usd": None,
        }

    return None


def _is_finished(results_dir: Path) -> bool:
    """True if a run has reached a terminal state.

    Terminal indicators (checked in order):
    - exit_code or run_exit_code files exist (modern runs)
    - summary.workflow_status in terminal states: completed, failed, killed, stopped, stopped_by_token_cap, running_at_cap
    - summary.txt exists (older runs that completed)
    """
    # Check for primary exit markers (modern runs)
    if (results_dir / "exit_code").exists() or (results_dir / "run_exit_code").exists():
        return True

    # Check summary.json for terminal workflow_status
    try:
        summary_file = results_dir / "summary.json"
        if summary_file.exists():
            summary = json.loads(summary_file.read_text())
            status = summary.get("workflow_status")
            if status in ("completed", "failed", "killed", "stopped", "stopped_by_token_cap", "running_at_cap"):
                return True
    except (OSError, json.JSONDecodeError):
        pass

    # Fallback: summary.txt means old run format that completed
    if (results_dir / "summary.txt").exists():
        return True

    return False


def _workflow_journals(results_dir: Path) -> list[Path]:
    """Every workflow journal of a results directory, in the order the run wrote them.

    Two archive shapes exist. The superseded launcher wrote one `discovery-journal.jsonl` (or `journal.jsonl`)
    per run. The phase pipeline launches one session per phase and writes one journal each,
    `NN-<phase>-journal.jsonl`; the `NN` prefix is the phase order, so sorting by name is chronological.

    Returning a list rather than the first match is the point: with a single path this endpoint answered
    "run not found" for every run of the current architecture, because it looked only for the old names.
    """
    for name in ("discovery-journal.jsonl", "journal.jsonl"):
        candidate = results_dir / name
        if candidate.is_file():
            return [candidate]
    return sorted(results_dir.glob("[0-9][0-9]-*-journal.jsonl"))


def _phase_streams(results_dir: Path) -> list[Path]:
    """The launcher streams of a phase-pipeline run, one per launched phase, in phase order."""
    return sorted(results_dir.glob("[0-9][0-9]-*-output.jsonl"))


def _read_json(path: Path) -> dict[str, Any] | None:
    """Parsed JSON object at `path`, or None when missing or unreadable: listing must never 500."""
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


# Pipeline stages driven by run.sh, in order. Each maps to the artifact that records its outcome:
# `<stage>-result.json` (written by pipeline/extract_workflow_result.py) for the workflow stages,
# `evaluation/evaluation.json` for the evaluation stage.
_STAGE_ORDER = ("ca", "discovery", "evaluation")
_WORKFLOW_STAGES = ("ca", "discovery")

StageStatus = Literal["passed", "failed", "running", "not_started"]


@dataclass(frozen=True)
class StageState:
    """Outcome of one pipeline stage of a run. `detail` is the raw recorded status or error, if any."""

    stage: str
    status: StageStatus
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"stage": self.stage, "status": self.status, "detail": self.detail}


def _pipeline_phases(results_dir: Path) -> list[str] | None:
    """The phases a run of the phase pipeline was launched with (`context.json` `phases`); None for any other run."""
    context = _read_json(results_dir / "context.json")
    phases = context.get("phases") if context else None
    if isinstance(phases, list) and all(isinstance(p, str) for p in phases):
        return phases
    return None


def _pipeline_run_alive(results_dir: Path) -> bool:
    """True while the orchestrator recorded in `run.pid` is alive and still identifies itself as this run.

    The orchestrator writes `run.pid` when it starts and removes it when it ends (`pipeline/stop.py`); the pid is
    trusted only if its process still names this run, never matched by pattern across the process table.
    """
    context = _read_json(results_dir / "context.json") or {}
    run_id = str(context.get("run_id") or results_dir.parent.name)
    identifies = default_identity(run_id, results_dir)
    return any(r.role == "orchestrator" and identifies(r.pid) for r in recorded_processes(results_dir))


def _pipeline_stages(results_dir: Path, phases: list[str]) -> list[StageState]:
    """Per-phase status of a phase-pipeline run, from `state.json` `completed`.

    A phase is recorded only once it completes, so a failure leaves no record of its own: in a run that has
    ended, the first phase not completed is where it stopped (failed or stopped by hand, which the archive does
    not tell apart). In a live run that phase is `running`.
    """
    state = _read_json(results_dir / "state.json") or {}
    completed = set(state.get("completed") or [])
    states = [StageState(phase, "passed" if phase in completed else "not_started") for phase in phases]
    first = next((i for i, s in enumerate(states) if s.status == "not_started"), None)
    if first is not None:
        if _pipeline_run_alive(results_dir):
            states[first] = StageState(phases[first], "running")
        else:
            states[first] = StageState(phases[first], "failed", "the run ended before this phase completed")
    return states


def _is_run_finished(results_dir: Path) -> bool:
    """A terminal marker exists (`_is_finished`), or the run is no longer within its time budget
    according to context.json (`is_run_in_progress`, the Overview live-progress check). A run that died
    before writing any marker is therefore finished, not live. A phase-pipeline run is finished when its
    orchestrator is no longer alive."""
    if _pipeline_phases(results_dir) is not None:
        return not _pipeline_run_alive(results_dir)
    if _is_finished(results_dir):
        return True
    context = _read_json(results_dir / "context.json")
    return context is not None and not is_run_in_progress(results_dir, context)


def run_stages(results_dir: Path) -> list[StageState]:
    """Ordered per-stage status of one run.sh results directory (the single source of stage logic).

    - workflow stages (ca, discovery): `<stage>-result.json` `status` "completed" -> passed, any other
      recorded status (failed, stopped, no_terminal_status, ...) -> failed. Without a record the stage
      is failed if `failed_stage` names it, otherwise not_started.
    - evaluation: passed when `evaluation/evaluation.json` exists, otherwise not_started.
    - the first stage without a record is `running` while the run is still live.

    A run of the phase pipeline is reported per phase instead (`_pipeline_stages`).
    """
    phases = _pipeline_phases(results_dir)
    if phases is not None:
        return _pipeline_stages(results_dir, phases)
    failed_stage = None
    try:
        failed_stage = (results_dir / "failed_stage").read_text().strip() or None
    except OSError:
        pass

    states: list[StageState] = []
    for stage in _STAGE_ORDER:
        if stage in _WORKFLOW_STAGES:
            record = _read_json(results_dir / f"{stage}-result.json")
            recorded = record.get("status") if record else None
            if recorded:
                status: StageStatus = "passed" if recorded == "completed" else "failed"
                detail = recorded if status == "failed" else None
                if status == "failed" and record.get("error"):
                    detail = f"{recorded}: {record['error']}"
                states.append(StageState(stage, status, detail))
            elif failed_stage == stage:
                states.append(StageState(stage, "failed", "failed_stage marker"))
            else:
                states.append(StageState(stage, "not_started"))
        else:
            done = (results_dir / "evaluation" / "evaluation.json").is_file()
            states.append(StageState(stage, "passed" if done else "not_started"))

    if all(s.status in ("passed", "not_started") for s in states) and not _is_run_finished(results_dir):
        for i, state in enumerate(states):
            if state.status == "not_started":
                states[i] = StageState(state.stage, "running")
                break
    return states


def _run_state(results_dir: Path) -> tuple[str, bool]:
    """(status, finished) of a smoke or run.sh results directory.

    Status, first found: summary.json `workflow_status`; the last recorded workflow stage
    (see `run_stages`, discovery before ca) `status`; run_exit_code (0 -> completed, else failed);
    otherwise "unknown". Finished: see `_is_run_finished`. A phase-pipeline run is `running` while its orchestrator
    lives, then `completed` when every phase it was launched with completed, `incomplete` otherwise.
    """
    phases = _pipeline_phases(results_dir)
    if phases is not None:
        if _pipeline_run_alive(results_dir):
            return "running", False
        completed = set((_read_json(results_dir / "state.json") or {}).get("completed") or [])
        return ("completed" if set(phases) <= completed else "incomplete"), True
    status = "unknown"
    summary = _read_json(results_dir / "summary.json")
    if summary and summary.get("workflow_status"):
        status = summary["workflow_status"]
    else:
        for stage in reversed(_WORKFLOW_STAGES):
            record = _read_json(results_dir / f"{stage}-result.json")
            if record and record.get("status"):
                status = record["status"]
                break
    if status == "unknown":
        try:
            status = "completed" if int((results_dir / "run_exit_code").read_text().strip()) == 0 else "failed"
        except (OSError, ValueError):
            pass

    return status, _is_run_finished(results_dir)


def scan_running_runs(experiments_root: Path, now: datetime | None = None) -> list[dict[str, Any]]:
    """Scan for currently running experiments.

    Returns at most _RUNNING_MAX_RESULTS runs, newest first.
    Checks terminal markers and liveness before reading journals.
    """
    now = now or datetime.now(timezone.utc)
    running = []

    if not experiments_root.is_dir():
        return running

    for experiment_dir in experiments_root.iterdir():
        if not experiment_dir.is_dir() or not is_safe_name(experiment_dir.name):
            continue

        results_dir = experiment_dir / "results"
        context_file = results_dir / "context.json"
        exit_code_file = results_dir / "exit_code"

        # Quick check: skip if terminal marker exists
        if exit_code_file.is_file():
            continue

        # Quick check: skip if context doesn't exist
        if not context_file.is_file():
            continue

        try:
            context = json.loads(context_file.read_text(encoding="utf-8"))

            # Check liveness before reading journals
            if not is_run_in_progress(results_dir, context, now=now):
                continue

            run_id = context.get("run_id", experiment_dir.name)
            start_time = context.get("started_utc", "")

            # Extract run metadata
            run_info = {
                "run_id": run_id,
                "type": "run.sh",  # All experiments/ runs are from run.sh
                "start_time": start_time,
                "stage": "unknown",
                "last_event_time": "",
                "last_event": "",
                "cost_usd_so_far": None,
                "tokens_so_far": None,
                "percent_complete": 0,
            }

            # Try to read phase and events from journal files
            phase_info = _extract_phase_from_journals(results_dir)
            if phase_info:
                run_info["stage"] = phase_info.get("stage", "unknown")
                run_info["last_event_time"] = phase_info.get("last_event_time", "")
                run_info["last_event"] = phase_info.get("last_event", "")
                run_info["percent_complete"] = phase_info.get("percent_complete", 0)

            # Try to extract cost and tokens from output files
            metrics = _extract_metrics_from_outputs(results_dir)
            if metrics:
                run_info["cost_usd_so_far"] = metrics.get("cost_usd", None)
                run_info["tokens_so_far"] = metrics.get("tokens", None)

            running.append(run_info)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            # Log warning but don't fail the whole scan
            print(f"⚠ Warning: could not read run {experiment_dir.name}: {exc}", flush=True)
            continue

    # Sort by start_time (newest first) and cap results
    running.sort(key=lambda r: r.get("start_time", ""), reverse=True)
    return running[:_RUNNING_MAX_RESULTS]


def is_run_in_progress(results_dir: Path, context: dict[str, Any], now: datetime | None = None) -> bool:
    """True if a run.sh run has no terminal marker and is still within its time budget; for a phase-pipeline run,
    while its orchestrator is alive."""
    if isinstance(context.get("phases"), list):
        return _pipeline_run_alive(results_dir)
    if any((results_dir / name).exists() for name in _TERMINAL_MARKERS):
        return False
    try:
        started = datetime.fromisoformat(str(context["started_utc"]).replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return False
    budget_s = int(context.get("ca_timeout_s", 0)) + int(context.get("discovery_timeout_s", 0)) + _RUNNING_GRACE_S
    now = now or datetime.now(timezone.utc)
    return (now - started).total_seconds() <= budget_s


def _mtime(path: Path) -> float:
    """Modification time, or 0.0 when the path cannot be read: ordering must never 500."""
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def make_handler(
    store: RunStore,
    catalog: RunCatalog | None = None,
    smoke_root: Path | None = None,
    experiments_root: Path | None = None,
    e2e_root: Path | None = None,
) -> type[BaseHTTPRequestHandler]:
    # One catalog answers "which runs exist and where" for every endpoint. The root parameters stay for
    # callers that pass them and feed that one object; `e2e_root` and `experiments_root` name the same
    # directory.
    if catalog is None:
        catalog = RunCatalog(
            experiments_root=experiments_root or e2e_root or EXPERIMENTS_ROOT,
            store=store,
            smoke_root=smoke_root or SMOKE_ROOT,
            state_fn=_run_state,
        )
    smoke_root = catalog.smoke_root
    experiments_root = catalog.experiments_root

    # Response cache for GET /runs/running with single-flight control
    cache_lock = threading.Lock()
    cache = {"at": 0.0, "data": []}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (http.server naming)
            path = urlparse(self.path).path
            query = parse_qs(urlparse(self.path).query)
            parts = [p for p in path.split("/") if p]

            try:
                if parts == []:
                    self._send_bytes(
                        HTTPStatus.OK, UI_FILE.read_bytes(), "text/html; charset=utf-8", cache_control="no-store"
                    )
                elif parts == ["runs"]:
                    self._send(HTTPStatus.OK, self._run_entries())
                elif parts == ["runs", "running"]:
                    self._handle_running_runs()
                elif len(parts) == 2 and parts[0] == "runs":
                    run_id = parts[1]
                    try:
                        # Try the run store first
                        context = store.read_context(run_id)
                    except RunNotFoundError:
                        # Fall back to the run's results directory (runs launched by run.sh)
                        results_dir = self._results_dir(run_id)
                        experiment_context = results_dir / "context.json" if results_dir else None
                        if experiment_context and experiment_context.is_file():
                            context = json.loads(experiment_context.read_text(encoding="utf-8"))
                        else:
                            raise
                    self._send(HTTPStatus.OK, context)
                elif len(parts) == 3 and parts[0] == "runs" and parts[2] == "stages":
                    results_dir = self._results_dir(parts[1])
                    if results_dir is None or not results_dir.is_dir():
                        raise RunNotFoundError(parts[1])
                    self._send(HTTPStatus.OK, [s.to_dict() for s in run_stages(results_dir)])
                elif len(parts) == 3 and parts[0] == "runs" and parts[2] == "story":
                    results_dir = self._results_dir(parts[1])
                    if results_dir is None or not results_dir.is_dir():
                        raise RunNotFoundError(parts[1])
                    self._send(HTTPStatus.OK, run_story(results_dir))
                elif len(parts) == 3 and parts[0] == "runs" and parts[2] == "events":
                    run_id = parts[1]
                    try:
                        events = store.read_events(run_id)
                    except RunNotFoundError:
                        # A run.sh run has a workflow journal, not store events: translate it
                        # instead of validating it as `Event`, which discarded every line (journal.py).
                        results_dir = self._results_dir(run_id)
                        journals = _workflow_journals(results_dir) if results_dir else []
                        if not journals:
                            raise
                        # One phase per journal: concatenating in phase order gives the run's own sequence.
                        merged: list[dict[str, Any]] = []
                        for journal in journals:
                            merged.extend(workflow_journal_events(journal))
                        self._send(HTTPStatus.OK, merged)
                        return
                    self._send(HTTPStatus.OK, [e.to_dict() for e in events])
                elif len(parts) == 3 and parts[0] == "runs" and parts[2] in ("phases", "roles"):
                    results_dir = self._results_dir(parts[1])
                    if results_dir is None or not results_dir.is_dir() or not has_phase_reports(results_dir):
                        raise RunNotFoundError(f"{parts[1]} (no phase reports: not a phase-pipeline run)")
                    if parts[2] == "phases":
                        stages = {s.stage: (s.status, s.detail) for s in run_stages(results_dir)}
                        self._send(HTTPStatus.OK, run_phases(results_dir, stages))
                    else:
                        self._send(HTTPStatus.OK, run_roles(results_dir, PROJECT_ROOT))
                elif len(parts) == 3 and parts[0] == "runs" and parts[2] == "metrics":
                    run_id = parts[1]
                    results_dir = self._results_dir(run_id)
                    if results_dir is not None and has_phase_reports(results_dir):
                        # A phase-pipeline run has no store events: its journals are not `Event` lines, and
                        # validating them as such discarded every line and reported 0 agent calls.
                        self._send(HTTPStatus.OK, pipeline_metrics(run_id, results_dir))
                        return
                    try:
                        events = store.read_events(run_id)
                    except RunNotFoundError:
                        # Fall back to the run's results directory
                        results_dir = self._results_dir(run_id)
                        if results_dir is not None:
                            journals = _workflow_journals(results_dir)
                            events_file = journals[0] if journals else results_dir / "journal.jsonl"
                            if events_file.is_file():
                                from .events import Event

                                events = []
                                with events_file.open(encoding="utf-8") as f:
                                    for line in f:
                                        if line.strip():
                                            try:
                                                events.append(Event.from_dict(json.loads(line)))
                                            except (json.JSONDecodeError, ValueError):
                                                pass
                            else:
                                raise
                        else:
                            raise
                    self._send(HTTPStatus.OK, summarize(run_id, events).to_dict())
                elif parts == ["tokens"]:
                    self._handle_tokens_list()
                elif parts == ["tokens-config"]:
                    self._handle_tokens_config()
                elif len(parts) == 2 and parts[0] == "tokens":
                    mode = query.get("mode", ["saved"])[0]
                    self._handle_tokens_run(parts[1], mode)
                elif len(parts) >= 2 and parts[0] == "static":
                    self._handle_static("/".join(parts[1:]))
                elif len(parts) == 2 and parts[0] == "evaluation":
                    self._handle_evaluation_run(parts[1])
                else:
                    self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})
            except RunNotFoundError as exc:
                self._send(HTTPStatus.NOT_FOUND, {"error": f"run not found: {exc}"})

        def _send(self, status: HTTPStatus, body: Any) -> None:
            self._send_bytes(
                status,
                json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8"),
                "application/json; charset=utf-8",
                cache_control="no-store",
            )

        def _send_bytes(
            self,
            status: HTTPStatus,
            payload: bytes,
            content_type: str,
            cache_control: str | None = None,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            if cache_control:
                self.send_header("Cache-Control", cache_control)
            self.end_headers()
            self.wfile.write(payload)

        def _handle_tokens_list(self) -> None:
            """Runs with their token availability, newest first.

            Same population as `GET /runs`, plus smoke runs, so a run offered by the selector is
            never one this endpoint answers 404 for.
            """
            runs = []
            for ref in catalog.list(include_smoke=True):
                streams = ("stream.jsonl", "discovery-output.jsonl", "ca-output.jsonl")
                runs.append(
                    {
                        "run_id": ref.id,
                        "kind": "smoke" if ref.source == "smoke" else "e2e",
                        "has_live": any((ref.results_dir / name).exists() for name in streams)
                        or bool(_phase_streams(ref.results_dir)),
                        # A phase-pipeline run's saved record is its phase reports.
                        "has_saved": (ref.results_dir / "tokens.json").exists() or has_phase_reports(ref.results_dir),
                        "status": ref.status,
                        "finished": ref.finished,
                    }
                )
            self._send(HTTPStatus.OK, runs)

        def _handle_tokens_run(self, run_id: str, mode: str) -> None:
            """Get token data for a run (live or saved). Supports both smoke and e2e runs."""
            # The catalog decides what a run id means; the two narrow id patterns this replaced
            # rejected valid ids such as `my-run-002` that `GET /runs` happily listed.
            ref = catalog.get(run_id)
            if ref is None:
                self._send(HTTPStatus.NOT_FOUND, {"error": "run not found"})
                return
            run_dir = ref.results_dir
            run_kind = "smoke" if ref.source == "smoke" else "e2e"

            try:
                if mode == "saved":
                    tokens_file = run_dir / "tokens.json"
                    if not tokens_file.exists() and has_phase_reports(run_dir):
                        run_status, finished = _run_state(run_dir)
                        self._send(HTTPStatus.OK, tokens_view(run_dir, run_status if finished else "running"))
                        return
                    if not tokens_file.exists():
                        self._send(
                            HTTPStatus.NOT_FOUND,
                            {"error": "saved snapshot not available; run tokens.json or wait for the run to finish"},
                        )
                        return
                    data = json.loads(tokens_file.read_text())
                    # Add legacy flag if no schema_version
                    if "schema_version" not in data:
                        data["legacy"] = True
                    self._send(HTTPStatus.OK, data)
                elif mode == "live":
                    # Load config for limits and prices
                    try:
                        config = load_tokens_config()
                    except FileNotFoundError as exc:
                        self._send(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"FileNotFoundError: {exc}"})
                        return

                    limits = config.get("limits", {})
                    prices = config.get("prices_usd_per_mtok", {})

                    # Load resolved config if available
                    resolved_config = {}
                    resolved_config_file = run_dir / "resolved_config.json"
                    if resolved_config_file.exists():
                        resolved_config = json.loads(resolved_config_file.read_text())

                    # Determine terminal status if run has finished
                    run_status, finished = _run_state(run_dir)
                    status = run_status if finished else "running"

                    # Smoke runs have one orchestrator stream; run.sh runs one per stage (discovery, ca);
                    # phase-pipeline runs one per launched phase (`NN-<phase>-output.jsonl`).
                    phase_streams = _phase_streams(run_dir)
                    if run_kind == "smoke":
                        stream_files = [run_dir / "stream.jsonl"]
                    elif phase_streams or has_phase_reports(run_dir):
                        stream_files = phase_streams
                    else:
                        stream_files = [run_dir / "discovery-output.jsonl", run_dir / "ca-output.jsonl"]
                    records, workflows = collect_usage(stream_files)
                    data = aggregate(records, limits, prices, resolved_config, source="live", status=status)
                    data["workflows"] = workflows
                    if phase_streams or has_phase_reports(run_dir):
                        if not records:
                            # Transcripts gone (or none yet): nothing was reconstructed, which is not zero.
                            data["totals"] = None
                        view = tokens_view(run_dir, status)
                        data.update({k: view[k] for k in ("by_phase", "phase_totals", "replay_of")})
                    self._send(HTTPStatus.OK, data)
                else:
                    self._send(HTTPStatus.BAD_REQUEST, {"error": f"invalid mode: {mode}"})
            except Exception as exc:
                self._send(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": f"{type(exc).__name__}: {exc}"},
                )

        def _handle_tokens_config(self) -> None:
            """Get polling interval for UI."""
            try:
                config = load_tokens_config()
                self._send(HTTPStatus.OK, {"poll_s": config.get("poll_s", 2)})
            except (FileNotFoundError, OSError) as exc:
                self._send(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"{type(exc).__name__}: {exc}"})

        def _handle_static(self, path: str) -> None:
            """Serve vendored static assets and the UI's own ES modules.

            The UI is a set of modules under `static/js/` rather than one inline script, so this
            serves a small tree instead of a single filename. Every segment is validated and the
            result is confined to STATIC_DIR: the path comes from the URL.
            """
            segments = [segment for segment in path.split("/") if segment]
            if not segments or not all(is_safe_name(segment) for segment in segments):
                self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            if not segments[-1].endswith(".js"):
                self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return

            filepath = STATIC_DIR.joinpath(*segments)
            try:
                filepath.resolve().relative_to(STATIC_DIR.resolve())
            except ValueError:
                self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            if not filepath.is_file():
                self._send(HTTPStatus.NOT_FOUND, {"error": "asset not found"})
                return
            self._send_bytes(
                HTTPStatus.OK,
                filepath.read_bytes(),
                "application/javascript; charset=utf-8",
                cache_control="no-store",
            )

        def _run_entries(self) -> list[dict[str, Any]]:
            """Runs for the UI selector, newest first, enriched so the selector needs no extra call.

            The catalog owns which runs exist; this adds only the per-run figures a row
            shows. `cost_usd` is what the CLI reported for the run, not the estimate reconstructed
            in tokens.json: the two differ and the UI labels them separately.
            """
            rows: list[dict[str, Any]] = []
            for ref in catalog.list():
                row = ref.to_dict()
                summary = _read_json(ref.results_dir / "summary.json") or {}
                usage = summary.get("usage") or {}
                row["cost_usd"] = summary.get("cost_usd")
                row["duration_ms"] = usage.get("duration_ms")
                row["total_tokens"] = usage.get("total_tokens")
                if not summary and has_phase_reports(ref.results_dir):
                    # A phase-pipeline run: the CLI's figures per launched phase, summed.
                    totals = run_totals(phase_usages(ref.results_dir))
                    row["cost_usd"] = totals["cost_usd"]
                    row["total_tokens"] = totals["total_tokens"]
                rows.append(row)
            return rows

        def _results_dir(self, run_id: str) -> Path | None:
            """Where one run's artifacts live, or None when the id is unknown or unsafe."""
            ref = catalog.get(run_id)
            return ref.results_dir if ref else None

        def _send_scorecard(self, results_dir: Path) -> None:
            from evaluation.scorecard import scorecard  # deferred: it reads the knowledge base

            context = _read_json(results_dir / "context.json") or {}
            kb = Path(str(context.get("kb") or ""))
            if not context.get("kb") or not kb.is_dir():
                self._send(HTTPStatus.NOT_FOUND, {"error": f"evaluation not found: knowledge base {kb} is missing"})
                return
            try:
                data = scorecard(results_dir, kb)
            except (OSError, ValueError, KeyError) as exc:  # shown, never hidden
                self._send(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"{type(exc).__name__}: {exc}"})
                return
            self._send(HTTPStatus.OK, {"kind": "scorecard", **data})

        def _evaluation_paths(self, run_id: str) -> list[Path]:
            """Candidate evaluation.json paths for one run.

            A store run id is not an experiment id: the catalog resolves it through the run context
            (`name`), so each run points at its own evaluation instead of a shared fallback.
            """
            results_dir = self._results_dir(run_id)
            return [results_dir / "evaluation" / "evaluation.json"] if results_dir else []

        def _handle_evaluation_run(self, run_id: str) -> None:
            """The scorecard of a phase-pipeline run, computed from its archive and knowledge base on each request;
            the recorded evaluation.json of a previous run.sh run."""
            results_dir = self._results_dir(run_id)
            if results_dir is not None and _pipeline_phases(results_dir) is not None:
                self._send_scorecard(results_dir)
                return
            try:
                eval_file = None
                for path in self._evaluation_paths(run_id):
                    if path.exists():
                        eval_file = path
                        break

                if not eval_file:
                    self._send(HTTPStatus.NOT_FOUND, {"error": "evaluation not found"})
                    return
                try:
                    data = json.loads(eval_file.read_text())
                    self._send(HTTPStatus.OK, data)
                except json.JSONDecodeError as exc:
                    self._send(
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"malformed JSON: {exc}"},
                    )
            except OSError as exc:
                self._send(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": f"OSError: {exc}"},
                )

        def _handle_running_runs(self) -> None:
            """List runs currently in progress with response caching and single-flight control."""
            # Scan under the lock (concurrent requests wait and reuse the result),
            # but send outside it so a slow client cannot block other requests.
            with cache_lock:
                now = monotonic()
                if now - cache["at"] >= _RUNNING_CACHE_TTL_S:
                    try:
                        cache["data"] = scan_running_runs(experiments_root)
                        cache["at"] = now
                    except Exception as exc:  # report, never kill the server thread
                        error = {"error": f"{type(exc).__name__}: {exc}"}
                    else:
                        error = None
                else:
                    error = None
                data = cache["data"]
            if error is not None:
                self._send(HTTPStatus.INTERNAL_SERVER_ERROR, error)
            else:
                self._send(HTTPStatus.OK, data)

    return Handler


def serve(
    store: RunStore,
    host: str = "127.0.0.1",
    port: int = 8765,
    smoke_root: Path | None = None,
    e2e_root: Path | None = None,
) -> None:
    # Absolute defaults: a relative one here resolves against whatever directory the server was started
    # from, which is how the UI came to list no experiment at all after the package was flattened.
    smoke_root = smoke_root or SMOKE_ROOT
    e2e_root = e2e_root or EXPERIMENTS_ROOT

    server = ThreadingHTTPServer((host, port), make_handler(store, smoke_root=smoke_root, e2e_root=e2e_root))
    server.daemon_threads = True
    print(f"Serving {store.root} on http://{host}:{port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
