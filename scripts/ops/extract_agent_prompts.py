#!/usr/bin/env python3
"""Recover the full prompts sent to each sub-agent, into the experiment's own results directory.

Why: `<stage>-result.json` (written by extract_workflow_result.py) records `transcript_dir`, the
harness's own per-run sub-agent transcript directory, e.g.
`~/.claude/projects/<project>/<session>/subagents/workflows/<wf_run_id>/`. Each sub-agent's full
conversation lives in `agent-<agentId>.jsonl` there, paired with `agent-<agentId>.meta.json`
(agentType, description/label, workflowPhase, model, spawnDepth). The stream files this repo
already archives (`<stage>-output.jsonl`, `stream.jsonl`) only carry a ~400-char `promptPreview`
per agent, truncated with an ellipsis: the full prompt text is not otherwise saved anywhere inside
`experiments/<run_id>/`.

That transcript directory lives outside the repository, in the CLI's own session storage: it is
not versioned, not guaranteed to persist across machines or cache eviction, and not referenced
from any file this repo commits. This script copies the full prompt text into the experiment's
results directory at run time, before that external copy can disappear.

Extraction: a sub-agent's genuine task prompt is the transcript's root turn — `type == "user"`,
`parentUuid == null`, `message.content` a plain string. Every later `"user"` turn in the same file
is a tool_result (a list), not an authored prompt. A file can hold more than one root turn only if
the harness restarted that agent slot (a retry); each is emitted as its own record with its own
`attempt` index rather than assuming exactly one.

Every prompt in this workflow architecture is constructed and dispatched by the workflow script
running inside one `Workflow` run, not by another sub-agent — sub-agents do not prompt each other
directly. `produced_by` records that fact (the workflow run, identified by its run id) rather than
inventing a peer-agent origin the transcript does not evidence. `to_whom` records the receiving
agent for symmetry and future extension (e.g. a harness version that also frames user-directed
turns), even though in the current architecture it is always the same agent as `agent_id`.

Writes:
    <results_dir>/prompts.jsonl   one JSON record per sub-agent prompt (with its sha256), across every stage found

Usage:
    extract_agent_prompts.py <results_dir> [--run-id ID]

Exit codes:
    0 always — this is a best-effort archival step and must never fail a run. Problems (a stage
    with no result file, a pruned transcript directory, an unreadable transcript line) are printed
    as warnings to stderr.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

# Workflow stages that may have a `<stage>-result.json` in a results directory, in the order
# run.sh executes them. Fixed list, not a glob: an unrelated `*-result.json` should not be treated
# as a workflow stage.
STAGES = ("ca", "discovery")


def read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    entries = []
    for lineno, raw in enumerate(path.read_text().splitlines(), 1):
        if not raw.strip():
            continue
        try:
            entries.append(json.loads(raw))
        except json.JSONDecodeError as err:
            print(f"warning: {path} line {lineno} is not JSON: {err}", file=sys.stderr)
    return entries


def find_prompt_turns(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every root task-prompt turn in one agent transcript, in file order."""
    turns = []
    for entry in entries:
        if entry.get("type") != "user" or entry.get("parentUuid") is not None:
            continue
        content = entry.get("message", {}).get("content")
        if isinstance(content, str):
            turns.append(entry)
    return turns


def extract_stage_prompts(results_dir: Path, stage: str, run_id: str) -> list[dict[str, Any]]:
    result = read_json(results_dir / f"{stage}-result.json")
    if result is None:
        return []  # stage did not run (e.g. discovery never launched) — not an error

    transcript_dir_str = result.get("transcript_dir")
    if not transcript_dir_str:
        print(f"warning: {stage}-result.json has no transcript_dir, cannot recover its prompts", file=sys.stderr)
        return []

    transcript_dir = Path(transcript_dir_str)
    if not transcript_dir.is_dir():
        print(
            f"warning: transcript_dir for stage '{stage}' not found on this machine "
            f"({transcript_dir}); its prompts cannot be recovered",
            file=sys.stderr,
        )
        return []

    # Layout: <project>/<session_id>/subagents/workflows/<workflow_run_id>/agent-<id>.jsonl
    workflow_run_id = transcript_dir.name
    session_id = transcript_dir.parent.parent.parent.name

    records: list[dict[str, Any]] = []
    for agent_file in sorted(transcript_dir.glob("agent-*.jsonl")):
        agent_id = agent_file.name[len("agent-") : -len(".jsonl")]
        meta = read_json(agent_file.with_suffix("").with_suffix(".meta.json")) or {}

        try:
            entries = load_jsonl(agent_file)
        except OSError as err:
            print(f"warning: could not read {agent_file}: {err}", file=sys.stderr)
            continue

        turns = find_prompt_turns(entries)
        if not turns:
            print(f"warning: no root prompt turn found in {agent_file}", file=sys.stderr)
            continue

        for attempt, turn in enumerate(turns, start=1):
            records.append(
                {
                    "run_id": run_id,
                    "stage": stage,
                    "workflow_run_id": workflow_run_id,
                    "session_id": session_id,
                    "agent_id": agent_id,
                    "attempt": attempt,
                    "label": meta.get("description"),
                    "agent_type": meta.get("agentType"),
                    "phase": meta.get("workflowPhase"),
                    "model": meta.get("model"),
                    "spawn_depth": meta.get("spawnDepth"),
                    "timestamp": turn.get("timestamp"),
                    "produced_by": f"workflow:{workflow_run_id}",
                    "to_whom": f"agent:{agent_id}",
                    "prompt": turn.get("message", {}).get("content"),
                    "prompt_sha256": hashlib.sha256(turn["message"]["content"].encode("utf-8")).hexdigest(),
                    "source_transcript": str(agent_file),
                }
            )

    return records


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("results_dir", type=Path)
    parser.add_argument(
        "--run-id", default=None, help="experiment run id to stamp on each record (default: results_dir's parent name)"
    )
    args = parser.parse_args(argv)

    run_id = args.run_id or args.results_dir.parent.name

    all_records: list[dict[str, Any]] = []
    for stage in STAGES:
        all_records.extend(extract_stage_prompts(args.results_dir, stage, run_id))

    all_records.sort(key=lambda r: (r.get("timestamp") or "", r.get("stage") or "", r.get("agent_id") or ""))

    out_path = args.results_dir / "prompts.jsonl"
    with out_path.open("w") as f:
        for record in all_records:
            f.write(json.dumps(record) + "\n")

    if all_records:
        print(f"wrote {len(all_records)} prompt record(s) to {out_path}")
    else:
        print("warning: no prompts recovered (no stage results, or transcripts unavailable)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
