"""What a phase-pipeline run executed, phase by phase, read only from its archive.

A run of the phase pipeline writes, per launched phase attempt `<stage>` (`NN-<phase>` or
`NN-<phase>.attemptK`):

- `<stage>-journal.jsonl`: one `started` line per agent the phase workflow spawned and one `result` line per
  agent that returned (`pipeline/extract_workflow_result.py`);
- `<stage>-agent-tools.jsonl`: one row per tool call those agents made, read from their transcripts, with
  inputs and results truncated (an audit trail, not a copy of the payload);
- `transcripts/<stage>/agent-<id>.jsonl`: the agents' transcripts themselves, archived only by runs made after
  that copy was added;
- `workflows/<stage>.args.json`: the arguments the phase workflow was launched with;
- `<stage>-result.json`: the workflow's outcome, with its one-line `summary` of the phase;

and per phase `NN-<phase>-report.json` (`phase_reports.py`), `state.json` (`completed`) and `context.json`
(`phases`, the plan). Every figure below is read from one of these; a file that is absent leaves its figure
absent (`None`), never zero.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from .phase_reports import PhaseUsage, phase_usages, replayed_phase

JOURNAL_SUFFIX = "-journal.jsonl"
TOOLS_SUFFIX = "-agent-tools.jsonl"
_ATTEMPT = re.compile(r"^(\d\d-[^.]+)(?:\.attempt\d+)?$")
# Arguments that are data handed to the agents (records, schemas, the KB slice), not settings of the role.
_NOT_SETTINGS = frozenset({"phase", "run_id", "objective", "schema"})


def _jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue  # a live run is appended to; a half-written last line is normal
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _attempt_key(stage: str) -> tuple[str, int]:
    """`05-review.attempt2` -> (`05-review`, 2); `05-review` -> (`05-review`, 1)."""
    base, _, attempt = stage.partition(".attempt")
    return base, int(attempt) if attempt.isdigit() else 1


def journal_stages(results_dir: Path) -> list[str]:
    """Every phase attempt that wrote a journal, in phase order and, within a phase, attempt order."""
    stages = [p.name.removesuffix(JOURNAL_SUFFIX) for p in results_dir.glob(f"[0-9][0-9]-*{JOURNAL_SUFFIX}")]
    return sorted((s for s in stages if _ATTEMPT.match(s)), key=_attempt_key)


def stage_agents(results_dir: Path, stage: str) -> list[dict[str, Any]]:
    """The agents one phase attempt started, in journal order, and whether each returned a result."""
    agents: dict[str, dict[str, Any]] = {}
    for row in _jsonl(results_dir / f"{stage}{JOURNAL_SUFFIX}"):
        agent_id = row.get("agentId")
        if not isinstance(agent_id, str) or not agent_id:
            continue
        if row.get("type") == "started" and agent_id not in agents:
            agents[agent_id] = {
                "agent_id": agent_id,
                "label": row.get("label"),
                "phase": row.get("phase"),
                "stage": stage,
                "finished": False,
            }
        elif row.get("type") == "result" and agent_id in agents:
            agents[agent_id]["finished"] = True
    return list(agents.values())


def stage_tool_calls(results_dir: Path, stage: str) -> dict[str, list[dict[str, Any]]] | None:
    """Tool calls of one phase attempt by agent id, each agent's in call order; None when no audit trail exists."""
    path = results_dir / f"{stage}{TOOLS_SUFFIX}"
    if not path.is_file():
        return None
    by_agent: dict[str, list[dict[str, Any]]] = {}
    for row in _jsonl(path):
        by_agent.setdefault(str(row.get("agent_id")), []).append(row)
    for calls in by_agent.values():
        calls.sort(key=lambda r: r.get("ordinal") if isinstance(r.get("ordinal"), int) else 0)
    return by_agent


def agent_prompt(results_dir: Path, stage: str, agent_id: str) -> tuple[str | None, str | None]:
    """(prompt, timestamp) of one agent: the first user message of its archived transcript, or (None, None)."""
    for row in _jsonl(results_dir / "transcripts" / stage / f"agent-{agent_id}.jsonl"):
        message = row.get("message")
        if row.get("type") == "user" and isinstance(message, dict) and isinstance(message.get("content"), str):
            timestamp = row.get("timestamp")
            return message["content"], timestamp if isinstance(timestamp, str) else None
    return None, None


def _attempts(usage: PhaseUsage, results_dir: Path) -> list[dict[str, Any]]:
    """Every attempt of a launched phase, in order: the earlier ones from their result records, then the reported one."""
    attempts = [{"stage": a["stage"], "status": a["status"], "cost_usd": a["cost_usd"]} for a in usage.earlier_attempts]
    attempts.append({"stage": usage.launch_stage, "status": usage.workflow_status, "cost_usd": usage.cost_usd})
    attempts.sort(key=lambda a: _attempt_key(str(a["stage"])))
    for attempt in attempts:
        agents = stage_agents(results_dir, str(attempt["stage"]))
        calls = stage_tool_calls(results_dir, str(attempt["stage"]))
        attempt["agents_started"] = len(agents)
        attempt["agents_finished"] = sum(a["finished"] for a in agents)
        attempt["tool_calls"] = None if calls is None else sum(len(c) for c in calls.values())
    return attempts


def _phase_status(phase: str, completed: set[str], stages: dict[str, tuple[str, str | None]]) -> tuple[str, str | None]:
    if phase in completed:
        return "completed", None
    if phase in stages:
        status, detail = stages[phase]
        return ("not_reached" if status == "not_started" else status), detail
    return "not_marked_completed", "the archive has a report for this phase but state.json does not list it"


def run_phases(results_dir: Path, stages: dict[str, tuple[str, str | None]]) -> dict[str, Any]:
    """The executed workflow as a chain of phases: per phase its status, attempts, agents, cost and tool calls.

    `stages` maps each planned phase to its status from `run_stages` (passed / failed / running / not_started).
    Reported phases come in their `NN` order; planned phases with no report follow in plan order (the pipeline
    runs phases one after another, so a phase without a report was never reached or is the one that stopped).
    Edges join consecutive phases: the pipeline is a sequence, and nothing else links phases in the archive.
    """
    context = _read_json(results_dir / "context.json") or {}
    planned = [p for p in context.get("phases") or [] if isinstance(p, str)]
    completed = {p for p in (_read_json(results_dir / "state.json") or {}).get("completed") or [] if isinstance(p, str)}
    phases: list[dict[str, Any]] = []
    for usage in phase_usages(results_dir):
        report = _read_json(results_dir / f"{usage.stage}-report.json") or {}
        status, detail = _phase_status(usage.phase, completed, stages)
        attempts = _attempts(usage, results_dir) if usage.launched else []
        last = attempts[-1] if attempts else {}
        outcome = report.get("status") if isinstance(report.get("status"), str) else None
        reason = report.get("reason") if isinstance(report.get("reason"), str) else None
        phases.append(
            {
                "phase": usage.phase,
                "stage": usage.stage,
                "planned": usage.phase in planned,
                "status": status,
                "detail": detail,
                "inherited": usage.inherited,
                "launched": usage.launched,
                "workflow_status": usage.workflow_status,
                "outcome": outcome,
                "outcome_reason": reason,
                "attempts": attempts,
                "agents_started": last.get("agents_started"),
                "agents_finished": last.get("agents_finished"),
                "agent_labels": (
                    [a["label"] for a in stage_agents(results_dir, usage.launch_stage)] if usage.launch_stage else []
                ),
                "tool_calls": last.get("tool_calls"),
                "cost_usd": _sum_present([a["cost_usd"] for a in attempts]),
                "wall_s": usage.wall_s,
            }
        )
    reported = {p["phase"] for p in phases}
    for phase in planned:
        if phase in reported:
            continue
        status, detail = _phase_status(phase, completed, stages)
        phases.append(
            {
                "phase": phase,
                "stage": None,
                "planned": True,
                "status": status,
                "detail": detail,
                "inherited": False,
                "launched": False,
                "workflow_status": None,
                "outcome": None,
                "outcome_reason": None,
                "attempts": [],
                "agents_started": None,
                "agents_finished": None,
                "agent_labels": [],
                "tool_calls": None,
                "cost_usd": None,
                "wall_s": None,
            }
        )
    edges = [{"from": a["phase"], "to": b["phase"]} for a, b in zip(phases, phases[1:])]
    return {"replay_of": replayed_phase(results_dir), "phases": phases, "edges": edges}


def _sum_present(values: list[Any]) -> float | None:
    present = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
    return sum(present) if present else None


def pipeline_metrics(run_id: str, results_dir: Path) -> dict[str, Any]:
    """The Overview's metrics of a phase-pipeline run, from its journals, tool-call audit trails and phase reports.

    Counted over the phases this archive ran itself: a replay archive's copied phases are listed apart, as in
    `phase_reports.run_totals`. Every attempt counts, since its agents were launched. A count whose source file
    exists in no attempt is None (absent), not 0.
    """
    usages = phase_usages(results_dir)
    inherited = {u.phase for u in usages if u.inherited}
    stages = [s for s in journal_stages(results_dir) if s.split("-", 1)[1].split(".")[0] not in inherited]
    by_phase: dict[str, dict[str, int]] = {}
    started = finished = 0
    for stage in stages:
        agents = stage_agents(results_dir, stage)
        phase = stage.split("-", 1)[1].split(".")[0]
        entry = by_phase.setdefault(phase, {"started": 0, "finished": 0})
        entry["started"] += len(agents)
        entry["finished"] += sum(a["finished"] for a in agents)
        started += len(agents)
        finished += sum(a["finished"] for a in agents)

    trails = [calls for s in stages if (calls := stage_tool_calls(results_dir, s)) is not None]
    rows = [row for calls in trails for agent_calls in calls.values() for row in agent_calls]
    statuses = Counter(str(r.get("status")) for r in rows)
    own = [u for u in usages if not u.inherited]
    return {
        "source": "phase_archive",
        "run_id": run_id,
        "phase_attempts": len(stages),
        "earlier_attempts": sum(len(u.earlier_attempts) for u in own),
        "agents_started": started if stages else None,
        "agents_finished": finished if stages else None,
        "agents_by_phase": by_phase,
        "tool_calls": len(rows) if trails else None,
        "tool_calls_by_tool": dict(Counter(str(r.get("tool")) for r in rows).most_common()) if trails else {},
        "tool_errors": statuses["error"] if trails else None,
        "tool_unanswered": statuses["missing"] if trails else None,
        "stages_without_tool_trail": [s for s in stages if not (results_dir / f"{s}{TOOLS_SUFFIX}").is_file()],
        "cost_usd": _sum_present([u.cost_usd for u in own] + [a["cost_usd"] for u in own for a in u.earlier_attempts]),
        "total_tokens": _sum_present([u.total_tokens for u in own]),
        "wall_s": _sum_present([u.wall_s for u in own]),
        "inherited_phases": sorted(inherited),
    }


def _settings(args: dict[str, Any]) -> dict[str, Any]:
    """The role's settings among a workflow's launch arguments: scalars, flat objects and lists of strings.

    Lists of records (the knowledge-base slice, the items to review) and the output schema are data, not settings.
    """
    out: dict[str, Any] = {}
    for key, value in args.items():
        if key in _NOT_SETTINGS:
            continue
        scalar = value is None or isinstance(value, (str, int, float, bool))
        flat = isinstance(value, dict) and all(
            isinstance(v, (str, int, float, bool)) or v is None for v in value.values()
        )
        strings = isinstance(value, list) and value and all(isinstance(v, str) for v in value)
        if scalar or flat or strings:
            out[key] = value
    return out


def _role_file(repo_root: Path, phase: str) -> dict[str, Any] | None:
    path = repo_root / "agents" / f"{phase}.md"
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return {"path": f"agents/{phase}.md", "sha256": hashlib.sha256(data).hexdigest(), "text": data.decode("utf-8")}


def _recorded_role_hashes(results_dir: Path, stage: str) -> dict[str, str] | None:
    """`role_files` of the run's per-phase manifest (ADR 0031), as {path: sha256}; None when not recorded."""
    try:
        manifest = json.loads((results_dir / "workflows" / f"{stage}.manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    entries = manifest if isinstance(manifest, list) else [manifest]
    hashes: dict[str, str] = {}
    for entry in entries:
        role_files = entry.get("role_files") if isinstance(entry, dict) else None
        if isinstance(role_files, list):
            for rf in role_files:
                if isinstance(rf, dict) and isinstance(rf.get("path"), str) and isinstance(rf.get("sha256"), str):
                    hashes[rf["path"]] = rf["sha256"]
        elif isinstance(role_files, dict):
            hashes.update({k: v for k, v in role_files.items() if isinstance(v, str)})
    return hashes or None


def _workflow_script(results_dir: Path, stage: str) -> str | None:
    """The archived workflow script of one phase attempt, relative to the results directory (two layouts exist)."""
    for rel in (f"workflows/{stage}.js", f"workflows/{stage}/{stage}.js"):
        if (results_dir / rel).is_file():
            return rel
    return None


def run_roles(results_dir: Path, repo_root: Path) -> list[dict[str, Any]]:
    """Per phase of the run: what the phase is for, the role text, the settings it ran with, and its agents.

    - `summary`: the phase workflow's own one-line description, from `<stage>-result.json` (archived per run);
    - `role`: `agents/<phase>.md` as it is in the repository now. Whether the run's agents received this text is
      known only when the run's manifest records `role_files` (ADR 0031): `role_match` is then true or false,
      otherwise None (runs made before ADR 0031 compiled their prompts into the workflow script itself,
      archived as `workflows/<stage>.js`);
    - `settings`: the scalar launch arguments of the phase workflow (`workflows/<stage>.args.json`);
    - `agents`: the agents the reported attempt started, with label and model (models from the tool-call trail).
    A phase that launched no workflow (`data`, `compute`) has no agents and says so.
    """
    roles: list[dict[str, Any]] = []
    for usage in phase_usages(results_dir):
        stage = usage.launch_stage
        role = _role_file(repo_root, usage.phase)
        if not usage.launched or stage is None:
            report = _read_json(results_dir / f"{usage.stage}-report.json") or {}
            outcome = report.get("status") if isinstance(report.get("status"), str) else None
            reason = report.get("reason") if isinstance(report.get("reason"), str) else None
            roles.append(
                {
                    "phase": usage.phase,
                    "stage": usage.stage,
                    "launched": False,
                    "role": role,
                    "outcome": outcome,
                    "outcome_reason": reason,
                }
            )
            continue
        result = _read_json(results_dir / f"{stage}-result.json") or {}
        args = _read_json(results_dir / "workflows" / f"{stage}.args.json")
        recorded = _recorded_role_hashes(results_dir, stage)
        role_match = None
        if recorded is not None and role is not None:
            match = next((h for p, h in recorded.items() if p.endswith(f"agents/{usage.phase}.md")), None)
            role_match = None if match is None else match == role["sha256"]
        calls = stage_tool_calls(results_dir, stage) or {}
        agents = [
            {
                "label": a["label"],
                "finished": a["finished"],
                "model": next((c.get("model") for c in calls.get(a["agent_id"], []) if c.get("model")), None),
            }
            for a in stage_agents(results_dir, stage)
        ]
        roles.append(
            {
                "phase": usage.phase,
                "stage": stage,
                "launched": True,
                "summary": result.get("summary") if isinstance(result.get("summary"), str) else None,
                "role": role,
                "role_match": role_match,
                "workflow_script": _workflow_script(results_dir, stage),
                "settings": _settings(args) if args is not None else None,
                "agents": agents,
            }
        )
    return roles
