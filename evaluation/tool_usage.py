"""Which tools the agents of a run actually called, read from the run archive.

The question "do the agents use the tools they were given?" is easy to answer wrongly by hand: a phase can
silently never call the knowledge-base tools its prompt names. This module makes the check repeatable.

It is read-only and offline: it reads `<stage>-agent-tools.jsonl` from a run's results directory, nothing else.

    uv run python -m evaluation.tool_usage <run-results-dir> [--expect mcp__data__] [--diagnose] [--format markdown]

With `--expect PREFIX` it exits 1 if no agent called a tool whose name starts with PREFIX, so a probe run can
fail loudly instead of being read optimistically.

`--diagnose` reports three plain signals, with no interpretation: a call repeated identically by the same agent
(e.g. a `ToolSearch` loop), a call the runtime answered with an error, and a call
answered with nothing. An empty result is not a fault — a search of an empty knowledge base returns `{"records":
[]}` — but a run where every search came back empty is worth seeing.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

SUFFIX = "-agent-tools.jsonl"


def read_calls(results_dir: Path) -> list[dict[str, Any]]:
    """Every recorded tool call of a run, in stage order. A stage without the file simply contributes nothing:
    the archive of an older run, or of a phase whose agents called no tool, is not an error."""
    calls: list[dict[str, Any]] = []
    for path in sorted(results_dir.glob(f"*{SUFFIX}")):
        stage = path.name[: -len(SUFFIX)]
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            calls.append({**row, "stage": stage})
    return calls


def summarize(calls: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Counts per tool, per stage and per agent label, plus the stages that called nothing at all."""
    calls = list(calls)
    per_tool: Counter[str] = Counter()
    per_stage: dict[str, Counter[str]] = {}
    per_agent: dict[str, Counter[str]] = {}
    for call in calls:
        tool = str(call.get("tool"))
        per_tool[tool] += 1
        per_stage.setdefault(call["stage"], Counter())[tool] += 1
        per_agent.setdefault(str(call.get("label") or call.get("agent_id")), Counter())[tool] += 1
    return {
        "calls": len(calls),
        "tools": dict(per_tool.most_common()),
        "stages": {stage: dict(counter.most_common()) for stage, counter in sorted(per_stage.items())},
        "agents": {agent: dict(counter.most_common()) for agent, counter in sorted(per_agent.items())},
    }


def callers_of(calls: Iterable[dict[str, Any]], prefix: str) -> dict[str, list[str]]:
    """Which agents called a tool whose name starts with `prefix`, keyed by stage. Empty means nobody did."""
    found: dict[str, list[str]] = {}
    for call in calls:
        if str(call.get("tool", "")).startswith(prefix):
            agents = found.setdefault(call["stage"], [])
            label = str(call.get("label") or call.get("agent_id"))
            if label not in agents:
                agents.append(label)
    return found


def _is_empty(preview: str, size: int) -> bool:
    """A result that carried no content: literally nothing, or a structure whose every value is empty.

    `{"records":[]}` from a knowledge-base search is the case worth seeing, and it is 14 bytes, so size alone
    cannot tell. The preview is truncated, so a parse failure means "not visibly empty", never "empty".
    """
    text = (preview or "").strip()
    if size <= 2 or text in ("", "{}", "[]", '""', "null"):
        return True
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return False
    if isinstance(value, str):  # a tool result is often a JSON string holding JSON
        return _is_empty(value, len(value))
    if isinstance(value, dict):
        return bool(value) and all(v in ([], {}, "", None, 0) for v in value.values())
    return isinstance(value, list) and not value


def diagnose(calls: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Repeated, failed and empty calls. Deliberately three simple checks over the archived rows, no inference."""
    calls = list(calls)
    seen: Counter[tuple[str, str, str]] = Counter()
    for call in calls:
        seen[
            (str(call.get("label") or call.get("agent_id")), str(call.get("tool")), str(call.get("input_preview", "")))
        ] += 1
    repeated = [
        {"agent": agent, "tool": tool, "times": times} for (agent, tool, _), times in seen.most_common() if times > 1
    ]
    failed = [
        {
            "agent": str(c.get("label") or c.get("agent_id")),
            "tool": c.get("tool"),
            "stage": c.get("stage"),
            "result": c.get("result_preview"),
        }
        for c in calls
        if c.get("status") == "error"
    ]
    # `result_bytes` is absent in archives written before the outcome was recorded; those rows cannot be judged.
    empty = [
        {"agent": str(c.get("label") or c.get("agent_id")), "tool": c.get("tool"), "stage": c.get("stage")}
        for c in calls
        if c.get("status") == "ok" and _is_empty(str(c.get("result_preview", "")), int(c.get("result_bytes") or 0))
    ]
    unknown = sum(1 for c in calls if "status" not in c)
    return {"repeated_calls": repeated, "failed_calls": failed, "empty_results": empty, "rows_without_outcome": unknown}


def render(
    summary: dict[str, Any],
    expected: dict[str, list[str]] | None = None,
    prefix: str = "",
    problems: dict[str, Any] | None = None,
) -> str:
    lines = [f"# Tool use — {summary['calls']} calls", "", "| tool | calls |", "|---|---|"]
    lines += [f"| `{tool}` | {count} |" for tool, count in summary["tools"].items()]
    lines += ["", "| stage | tools |", "|---|---|"]
    lines += [
        f"| {stage} | " + ", ".join(f"`{t}`×{c}" for t, c in tools.items()) + " |"
        for stage, tools in summary["stages"].items()
    ]
    if prefix:
        lines += ["", f"## Calls to `{prefix}*`", ""]
        lines += [f"- {stage}: {', '.join(agents)}" for stage, agents in (expected or {}).items()] or [
            f"- none: no agent called a tool starting with `{prefix}`"
        ]
    if problems is not None:
        lines += ["", "## Diagnosis", ""]
        for item in problems["repeated_calls"]:
            lines.append(f"- repeated: {item['agent']} called `{item['tool']}` {item['times']}x with the same input")
        for item in problems["failed_calls"]:
            lines.append(f"- error: {item['agent']} / `{item['tool']}` ({item['stage']}): {item['result']}")
        for item in problems["empty_results"]:
            lines.append(f"- empty: {item['agent']} / `{item['tool']}` ({item['stage']}) returned nothing")
        if problems["rows_without_outcome"]:
            lines.append(
                f"- {problems['rows_without_outcome']} row(s) carry no outcome: archive written before "
                "outcomes were recorded, so errors and empty results cannot be judged"
            )
        if not any(problems[key] for key in ("repeated_calls", "failed_calls", "empty_results")):
            lines.append("- nothing to report")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("results_dir", type=Path, help="experiments/<RUN_ID>/results")
    parser.add_argument("--expect", default="", help="fail if no agent called a tool with this name prefix")
    parser.add_argument("--diagnose", action="store_true", help="also report repeated, failed and empty calls")
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    args = parser.parse_args(argv)
    if not args.results_dir.is_dir():
        print(f"tool_usage: no such directory: {args.results_dir}", file=sys.stderr)
        return 2
    calls = read_calls(args.results_dir)
    summary = summarize(calls)
    found = callers_of(calls, args.expect) if args.expect else {}
    problems = diagnose(calls) if args.diagnose else None
    print(
        render(summary, found, args.expect, problems)
        if args.format == "markdown"
        else json.dumps(
            {
                **summary,
                **({"expected": found} if args.expect else {}),
                **({"diagnosis": problems} if problems else {}),
            },
            indent=2,
        )
    )
    if args.expect and not found:
        print(f"tool_usage: no agent called a tool starting with {args.expect!r}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
