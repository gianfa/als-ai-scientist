"""Read a workflow run's journal into events the UI can display.

Two journal formats exist and they are not interchangeable:

- the run store's `events.jsonl`, whose lines are `events.Event` records;
- the workflow runtime's `journal.jsonl`, written for every `run.sh` run, whose lines look like
  `{"type": "started", "agentId": ..., "label": ..., "phase": ...}`.

Feeding the second through `Event.from_dict` would not work: it rejects unknown fields with
`InvalidEventError` (a `ValueError`), so every journal line would be discarded in silence and the
endpoint would return `[]` while looking like it worked. `Event` also requires `run_id` to match the
store's timestamp pattern, which a workflow run id need not.

So this translates instead of validating. It also performs a join that is easy to miss: a `result` line carries no `label`, only the `agentId` of its `started` line, so
anything that filters results by label without joining first silently finds nothing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

KIND_BY_TYPE = {"launched": "run_started", "started": "agent_started", "result": "agent_finished"}


def workflow_journal_events(path: Path) -> list[dict[str, Any]]:
    """Ordered display events from a workflow `journal.jsonl`, or [] when it is absent.

    Malformed lines are skipped — a journal is appended to while the run is live, so a truncated
    last line is normal — but an unrecognised `type` is kept with its raw kind rather than dropped,
    so a new runtime event shows up as unknown instead of vanishing.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []

    labels: dict[str, str] = {}
    phases: dict[str, str] = {}
    events: list[dict[str, Any]] = []

    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue

        agent_id = entry.get("agentId")
        if entry.get("type") == "started" and agent_id:
            if entry.get("label"):
                labels[agent_id] = str(entry["label"])
            if entry.get("phase"):
                phases[agent_id] = str(entry["phase"])

        result = entry.get("result")
        events.append(
            {
                "kind": KIND_BY_TYPE.get(str(entry.get("type")), str(entry.get("type"))),
                "agent": labels.get(agent_id) if agent_id else None,
                "agent_id": agent_id,
                "phase": entry.get("phase") or (phases.get(agent_id) if agent_id else None),
                # The payload can be a whole proposal; the UI wants a hint, not a document.
                "summary": _summarize(result),
            }
        )
        graph = _causal_graph(result)
        if graph is not None:
            events[-1]["causal_graph"] = graph
    return events


def _causal_graph(result: Any) -> dict[str, Any] | None:
    """The full causal-graph payload (`nodes` + `edges`), recognised by shape, else None.

    `summary` is cut at 300 characters, which is not enough to draw the graph; this carries the
    complete structure for the one payload the UI renders rather than prints.
    """
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except json.JSONDecodeError:
            return None
    if isinstance(result, dict) and isinstance(result.get("nodes"), list) and isinstance(result.get("edges"), list):
        return result
    return None


def _summarize(result: Any, limit: int = 300) -> str | None:
    if result is None:
        return None
    text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + "…"
