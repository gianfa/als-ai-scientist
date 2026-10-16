"""Read-only statistics over the knowledge base and the pipeline run archives.

Diagnoses after the fact need versioned, reproducible code rather than throw-away queries.
This module computes them from the persisted artifacts only: the KB JSONL files and `experiments/<run>/results/`.
It writes nothing, infers nothing an artifact does not state, and never opens the KB for writing.

    uv run python -m evaluation.kb_stats --kb ../knowledgebase --runs ../experiments
    uv run python -m evaluation.kb_stats --format markdown

Three views:

- `kb_stats(records)`: what the KB holds — records by type and run, claim provenance and epistemic status,
  relation justification, and the yield of the verification step (how many agent-inferred links became verified).
- `event_stats(records)`: what the KB refused — `rejected` and `seen_again` events by agent and by reason, with
  the reason text folded to its leading clause so that recurring faults group together.
- `run_stats(run_dir)`: what a run cost and dropped — per phase the launcher status, cost, wall time, agents, and
  the records written, dropped or rejected, as `state.json` and the phase reports recorded them.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from knowledge_base.schemas import Claim, Decision, Event, Experiment, Hypothesis, Relation
from knowledge_base.status import statuses, superseded
from knowledge_base.store import read_records
from knowledge_base.validate import Record

KIND_OF: dict[str, str] = {
    "C": "claim",
    "L": "relation",
    "N": "entity",
    "H": "hypothesis",
    "I": "ignorance",
    "V": "event",
    "EXP": "experiment",
    "D": "decision",
    "INT": "intervention",
}
# A reason reads "contradicts L-42 starts from unverified C-4: record it as questions instead". Dropping the IDs
# and keeping the leading clause makes the recurring fault, not the record it happened on, the unit of counting.
_IDS = re.compile(r"\b(?:C|L|N|H|I|V|EXP|D|INT)-\d+\b")


def _kind(record_id: str) -> str:
    return KIND_OF.get(record_id.rsplit("-", 1)[0], "unknown")


def reason_class(reason: str | None) -> str:
    """The reason text with record IDs blanked and the trailing advice cut, so like faults count together."""
    if not reason:
        return "(none)"
    return _IDS.sub("<id>", reason).split(":")[0].strip()


def kb_stats(records: Mapping[str, Record]) -> dict[str, Any]:
    """What the KB holds, counted by type, run, provenance and computed epistemic status."""
    old = superseded(records)
    status = statuses(records)
    claims = [r for r in records.values() if isinstance(r, Claim)]
    relations = [r for r in records.values() if isinstance(r, Relation)]
    current_claims = [c for c in claims if c.id not in old]
    verified_links = {
        (r.source_id, r.target_id, r.type)
        for r in relations
        if r.type in ("supports", "contradicts") and r.provenance_status == "verified"
    }
    inferred = [
        r for r in relations if r.type in ("supports", "contradicts") and r.justification_method == "agent_inference"
    ]
    return {
        "records": dict(Counter(_kind(r.id) for r in records.values())),
        "records_by_run": dict(Counter(r.run_id for r in records.values())),
        "runs": sorted({r.run_id for r in records.values()}),
        "claims": {
            "total": len(claims),
            "current": len(current_claims),
            "superseded": len(claims) - len(current_claims),
            "provenance_status": dict(Counter(c.provenance_status for c in current_claims)),
            "verification_method": dict(Counter(c.verification_method for c in current_claims)),
            "origin": dict(Counter(c.origin for c in current_claims)),
            "type": dict(Counter(c.type for c in current_claims)),
            # Computed, never stored: see knowledge_base/status.py.
            "epistemic_status": dict(Counter(status.get(c.id, "unresolved") for c in current_claims)),
        },
        "relations": {
            "total": len(relations),
            "type": dict(Counter(r.type for r in relations)),
            "justification_method": dict(Counter(r.justification_method for r in relations)),
            "provenance_status": dict(Counter(r.provenance_status for r in relations)),
        },
        "verification_yield": {
            "agent_inferred_links": len(inferred),
            "confirmed_as_verified": sum(1 for r in inferred if (r.source_id, r.target_id, r.type) in verified_links),
        },
        "hypotheses": {
            "total": sum(1 for r in records.values() if isinstance(r, Hypothesis)),
            "by_run": dict(Counter(r.run_id for r in records.values() if isinstance(r, Hypothesis))),
            "decided": dict(Counter(r.decision for r in records.values() if isinstance(r, Decision))),
        },
        "experiments": {
            "total": sum(1 for r in records.values() if isinstance(r, Experiment)),
            "by_run": dict(Counter(r.run_id for r in records.values() if isinstance(r, Experiment))),
        },
    }


def event_stats(records: Mapping[str, Record]) -> dict[str, Any]:
    """What the KB refused or recognised again, by event type, agent and folded reason."""
    events = [r for r in records.values() if isinstance(r, Event)]
    out: dict[str, Any] = {"total": len(events), "event": dict(Counter(e.event for e in events)), "by_event": {}}
    for kind in ("rejected", "seen_again"):
        of_kind = [e for e in events if e.event == kind]
        out["by_event"][kind] = {
            "total": len(of_kind),
            "by_run": dict(Counter(e.run_id for e in of_kind)),
            "by_agent": dict(Counter(e.agent_id for e in of_kind)),
            "by_reason": dict(Counter(reason_class(e.detail) for e in of_kind)),
        }
    out["agents"] = len({e.agent_id for e in events})
    return out


def _counted(value: Any) -> int:
    """A report field is a list or a mapping of dropped items; `state.json` already stores its length."""
    if isinstance(value, (list, dict)):
        return len(value)
    return int(value or 0)


def run_stats(run_dir: Path) -> dict[str, Any]:
    """Cost, duration and per-phase yield of one run, from `state.json` and `context.json`. No LLM, no network."""
    results = run_dir / "results" if (run_dir / "results").is_dir() else run_dir
    state = json.loads((results / "state.json").read_text())
    context_path = results / "context.json"
    context = json.loads(context_path.read_text()) if context_path.is_file() else {}
    phases: dict[str, Any] = {}
    cost = wall = 0.0
    for name, phase in state.get("phases", {}).items():
        launch = phase.get("launch", {})
        phase_cost = float(launch.get("session_cost_usd") or 0.0)
        phase_wall = float(launch.get("wall_s") or 0.0)
        cost += phase_cost
        wall += phase_wall
        phases[name] = {
            "workflow_status": launch.get("workflow_status"),
            "launcher_exit": launch.get("launcher_exit"),
            "cost_usd": round(phase_cost, 4),
            "wall_s": phase_wall,
            "agents": launch.get("agent_count"),
            "tool_uses": (launch.get("workflow_usage") or {}).get("tool_uses"),
            "written": _counted(phase.get("written")),
            "dropped_agents": _counted(phase.get("dropped_agents")),
            "invalid_outputs": _counted(phase.get("invalid_outputs")),
            "dropped_elements": _counted(phase.get("dropped_elements")),
            "rejected": _counted(phase.get("rejected")),
        }
    return {
        "run_id": state.get("run_id"),
        "git_commit": context.get("git_commit"),
        "config": context.get("config"),
        "config_hash": state.get("config_hash"),
        "config_changes": state.get("config_changes", []),
        "completed": state.get("completed", []),
        "cost_usd": round(cost, 4),
        "wall_s": round(wall, 1),
        "phases": phases,
    }


def all_runs(runs_root: Path) -> list[dict[str, Any]]:
    """Every run archive under `runs_root` that has a `state.json`, oldest directory name first."""
    return [
        run_stats(d) for d in sorted(runs_root.iterdir()) if d.is_dir() and (d / "results" / "state.json").is_file()
    ]


def collect(kb_root: Path, runs_root: Path | None) -> dict[str, Any]:
    records = read_records(kb_root)
    report: dict[str, Any] = {"kb_root": str(kb_root), "kb": kb_stats(records), "events": event_stats(records)}
    if runs_root is not None and runs_root.is_dir():
        report["runs"] = all_runs(runs_root)
    return report


def _table(title: str, counts: Mapping[str, Any]) -> list[str]:
    rows = [f"| {k} | {v} |" for k, v in counts.items()]
    return [f"**{title}**", "", "| key | count |", "|---|---|", *rows, ""] if rows else []


def as_markdown(report: dict[str, Any]) -> str:
    kb, events = report["kb"], report["events"]
    lines = ["# Knowledge base and run statistics", "", f"KB: `{report['kb_root']}`", ""]
    lines += _table("Records by type", kb["records"])
    lines += _table("Records by run", kb["records_by_run"])
    lines += _table("Current claims by provenance", kb["claims"]["provenance_status"])
    lines += _table("Current claims by epistemic status", kb["claims"]["epistemic_status"])
    lines += _table("Relations by justification", kb["relations"]["justification_method"])
    yield_ = kb["verification_yield"]
    lines += [
        f"Verification yield: {yield_['confirmed_as_verified']} of {yield_['agent_inferred_links']} "
        "agent-inferred links are covered by a verified relation.",
        "",
    ]
    lines += _table("Rejected records by reason", events["by_event"]["rejected"]["by_reason"])
    for run in report.get("runs", []):
        lines += [
            "",
            f"## {run['run_id']} — {run['cost_usd']} USD, {run['wall_s']} s, "
            f"commit `{(run['git_commit'] or '')[:7]}`",
            "",
            "| phase | status | cost USD | wall s | agents | written | dropped agents | dropped elements |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for name, p in run["phases"].items():
            lines.append(
                f"| {name} | {p['workflow_status']} | {p['cost_usd']} | {p['wall_s']} | {p['agents']} "
                f"| {p['written']} | {p['dropped_agents']} | {p['dropped_elements']} |"
            )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    repo_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Read-only statistics over the KB and the run archives")
    parser.add_argument("--kb", type=Path, default=repo_root / "knowledgebase")
    parser.add_argument(
        "--runs",
        type=Path,
        default=repo_root / "experiments",
        help="root holding <run-id>/results/state.json; omitted runs are simply not reported",
    )
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    parser.add_argument("--out", type=Path, help="write to this file instead of stdout")
    args = parser.parse_args(argv)
    report = collect(args.kb, args.runs)
    text = as_markdown(report) if args.format == "markdown" else json.dumps(report, indent=2) + "\n"
    if args.out:
        args.out.write_text(text)
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
