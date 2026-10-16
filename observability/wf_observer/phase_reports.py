"""Cost and tokens of a phase-pipeline run, read from its per-phase reports.

A run of the phase pipeline writes no `summary.json` and no `tokens.json`. What it does write, per launched
phase, is `NN-<phase>-report.json`, whose `launch` block (`pipeline/launcher.py`) carries:

- `session_cost_usd`: the launched CLI session's `total_cost_usd`, which covers the workflow's agents;
- `workflow_usage`: the workflow's own usage as the CLI's task notification reported it (`total_tokens`,
  `tool_uses`, `duration_ms`), with no input/output split;
- `launcher_usage`: the launcher session's own turns (input, cache write, cache read, output).

The two token figures are taken as disjoint (the launcher's turns versus the workflow agents' turns), so a
phase's total is their sum. Phases that launch no workflow (`data`, `compute`) have no `launch` block: their
figures are absent, never zero. A phase whose figure is absent contributes nothing to a sum, and a sum over
no recorded figure is absent too.

A phase re-attempted after a failure keeps one report, describing the attempt that produced it
(`launch.stage`, e.g. `05-review.attempt2`). The earlier attempts' spend is recorded only in their
`<stage>-result.json` (`cost_usd`, and `usage.total_tokens` when the workflow reported it): it is listed per
phase as `earlier_attempts` and counted in the run's totals, since it was spent.

A replay archive (`state.json` `replay_of`) holds copies of the original run's reports for every phase but the
one it re-ran; those phases are marked `inherited` and left out of the run's totals, which therefore state
what this archive's own launches cost.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

REPORT_GLOB = "[0-9][0-9]-*-report.json"
_STAGE = re.compile(r"^(\d\d)-(.+)-report$")
LAUNCHER_KEYS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")


@dataclass(frozen=True)
class PhaseUsage:
    """What one phase report says its launch cost. None means the report does not record it."""

    stage: str
    phase: str
    launch_stage: str | None
    launched: bool
    inherited: bool
    workflow_status: str | None
    agent_count: int | None
    wall_s: float | None
    cost_usd: float | None
    workflow_tokens: int | None
    launcher_tokens: dict[str, int] | None
    launcher_total: int | None
    total_tokens: int | None
    earlier_attempts: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _sum(values: list[float | int | None]) -> float | int | None:
    present = [v for v in values if v is not None]
    return sum(present) if present else None


def has_phase_reports(results_dir: Path) -> bool:
    return any(results_dir.glob(REPORT_GLOB))


def replayed_phase(results_dir: Path) -> str | None:
    """The phase a replay archive re-ran (`state.json` `replay_of.phase`), or None for an ordinary run."""
    replay = (_read_json(results_dir / "state.json") or {}).get("replay_of")
    phase = replay.get("phase") if isinstance(replay, dict) else None
    return phase if isinstance(phase, str) and phase else None


def _earlier_attempts(results_dir: Path, stage: str, launch_stage: str) -> tuple[dict[str, Any], ...]:
    """Attempts of `stage` other than the one the report describes, with what their result records say."""
    paths = [results_dir / f"{stage}-result.json", *sorted(results_dir.glob(f"{stage}.attempt*-result.json"))]
    attempts = []
    for path in paths:
        attempt = path.name.removesuffix("-result.json")
        record = _read_json(path)
        if attempt == launch_stage or record is None:
            continue
        usage = record.get("usage")
        attempts.append(
            {
                "stage": attempt,
                "status": record.get("status") if isinstance(record.get("status"), str) else None,
                "cost_usd": _number(record.get("cost_usd")),
                "workflow_tokens": _count(usage.get("total_tokens")) if isinstance(usage, dict) else None,
            }
        )
    return tuple(attempts)


def _phase_usage(path: Path, replayed: str | None) -> PhaseUsage | None:
    match = _STAGE.match(path.stem)
    report = _read_json(path)
    if match is None or report is None:
        return None
    stage, phase = f"{match.group(1)}-{match.group(2)}", match.group(2)
    inherited = replayed is not None and phase != replayed
    launch = report.get("launch")
    if not isinstance(launch, dict):
        return PhaseUsage(stage, phase, None, False, inherited, None, None, None, None, None, None, None, None)
    launch_stage = launch.get("stage") if isinstance(launch.get("stage"), str) else stage

    workflow_usage = launch.get("workflow_usage")
    workflow_tokens = _count(workflow_usage.get("total_tokens")) if isinstance(workflow_usage, dict) else None
    raw_launcher = launch.get("launcher_usage")
    launcher_tokens: dict[str, int] | None = None
    if isinstance(raw_launcher, dict):
        launcher_tokens = {k: v for k in LAUNCHER_KEYS if (v := _count(raw_launcher.get(k))) is not None} or None
    launcher_total = sum(launcher_tokens.values()) if launcher_tokens else None
    status = launch.get("workflow_status")
    return PhaseUsage(
        stage=stage,
        phase=phase,
        launch_stage=launch_stage,
        launched=True,
        inherited=inherited,
        workflow_status=status if isinstance(status, str) else None,
        agent_count=_count(launch.get("agent_count")),
        wall_s=_number(launch.get("wall_s")),
        cost_usd=_number(launch.get("session_cost_usd")),
        workflow_tokens=workflow_tokens,
        launcher_tokens=launcher_tokens,
        launcher_total=launcher_total,
        total_tokens=_sum([workflow_tokens, launcher_total]),  # type: ignore[arg-type]
        earlier_attempts=_earlier_attempts(path.parent, stage, launch_stage),
    )


def phase_usages(results_dir: Path) -> list[PhaseUsage]:
    """One entry per phase report, in phase order; an unreadable report is skipped, never guessed."""
    replayed = replayed_phase(results_dir)
    usages = (_phase_usage(path, replayed) for path in sorted(results_dir.glob(REPORT_GLOB)))
    return [u for u in usages if u is not None]


def run_totals(usages: list[PhaseUsage]) -> dict[str, float | int | None]:
    """Cost, tokens and wall time summed over the phases this archive launched itself (not inherited).

    Earlier attempts count towards cost and tokens; their wall time and launcher tokens are not recorded.
    """
    own = [u for u in usages if not u.inherited]
    earlier = [a for u in own for a in u.earlier_attempts]
    return {
        "cost_usd": _sum([u.cost_usd for u in own] + [a["cost_usd"] for a in earlier]),
        "total_tokens": _sum([u.total_tokens for u in own] + [a["workflow_tokens"] for a in earlier]),
        "workflow_tokens": _sum([u.workflow_tokens for u in own] + [a["workflow_tokens"] for a in earlier]),
        "launcher_tokens": _sum([u.launcher_total for u in own]),
        "wall_s": _sum([u.wall_s for u in own]),
    }


def tokens_view(results_dir: Path, status: str) -> dict[str, Any]:
    """The Tokens tab's data for a phase-pipeline run: per-phase figures as the reports recorded them."""
    usages = phase_usages(results_dir)
    return {
        "source": "phase_reports",
        "status": status,
        "replay_of": replayed_phase(results_dir),
        "by_phase": [u.to_dict() for u in usages],
        "phase_totals": run_totals(usages),
    }
