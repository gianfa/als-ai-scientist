"""Run metrics computed only from persisted events.

A total is None when no event recorded that value: missing data is never reported as zero.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from statistics import median
from typing import Any

from .events import Event


@dataclass(frozen=True)
class RunMetrics:
    run_id: str
    events: int
    agent_calls: int
    agent_calls_by_agent: dict[str, int]
    tool_calls: int
    errors: int
    gates: dict[str, int]  # "<gate>:<outcome>" -> count
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    latency_ms_total: float | None
    latency_ms_median: float | None
    latency_ms_max: float | None
    finished: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _total(values: list[float]) -> float | None:
    return sum(values) if values else None


def summarize(run_id: str, events: list[Event]) -> RunMetrics:
    finished_agents = [e for e in events if e.kind == "agent_finished"]
    latencies = [e.latency_ms for e in finished_agents if e.latency_ms is not None]
    input_tokens = [e.input_tokens for e in events if e.input_tokens is not None]
    output_tokens = [e.output_tokens for e in events if e.output_tokens is not None]
    costs = [e.cost_usd for e in events if e.cost_usd is not None]
    gates = Counter(f"{e.data.get('gate', '?')}:{e.data.get('outcome', '?')}" for e in events if e.kind == "gate")
    return RunMetrics(
        run_id=run_id,
        events=len(events),
        agent_calls=len(finished_agents),
        agent_calls_by_agent=dict(Counter(e.agent or "?" for e in finished_agents)),
        tool_calls=sum(e.kind == "tool_call" for e in events),
        errors=sum(e.kind == "error" for e in events),
        gates=dict(gates),
        input_tokens=_total(input_tokens),
        output_tokens=_total(output_tokens),
        cost_usd=_total(costs),
        latency_ms_total=_total(latencies),
        latency_ms_median=median(latencies) if latencies else None,
        latency_ms_max=max(latencies) if latencies else None,
        finished=any(e.kind == "run_finished" for e in events),
    )
