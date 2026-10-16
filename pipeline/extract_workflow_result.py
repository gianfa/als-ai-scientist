#!/usr/bin/env python3
"""Extract what actually happened in a background workflow from a Claude stream.

The `Workflow` tool returns as soon as the workflow is launched, so its tool result is only a launch
acknowledgement. The real outcome arrives later, in the stream's background-task events:

- `system/task_started`      binds the Workflow tool call to a task id
- `system/task_updated`      carries the terminal status and, on failure, the error
- `system/task_notification` carries the status, usage, and the path of the task output file
- the task output file       holds `{summary, agentCount, logs, result}` — `result` is the
                             workflow's own return value

Reading the tool result instead reported a failed workflow as a success, so this script reads the
task events and fails loudly when the workflow did not complete.

Usage:
    extract_workflow_result.py <stream.jsonl> <results_dir> --stage <name> [--exit-code N]

Writes:
    <results_dir>/<stage>-result.json   full record: status, error, usage, logs, result
    <results_dir>/<stage>-journal.jsonl the workflow journal, when the transcript is available
    <results_dir>/<stage>-agent-tools.jsonl one row per tool call made by the workflow's agents, with its
                             outcome, when their transcripts are available (the stream only holds the launcher's
                             own calls)
    <results_dir>/transcripts/<stage>/agent-*.jsonl the agents' transcripts themselves, copied out of the
                             harness's session storage, which deletes them; each one's size and sha256 go in
                             <stage>-result.json. Suppress with --no-archive-transcripts.

and, for the stage named by --evaluated-stage (default: discovery), the summary artifacts the observer UI
reads:

    <results_dir>/result.json           the workflow's own return value
    <results_dir>/summary.json          workflow_status, workflow_error, agent counts, cost
    <results_dir>/stream.jsonl          the stream, copied
    <results_dir>/journal.jsonl         the workflow journal, copied
    <results_dir>/exit_code             the launcher's exit code for the stage

Exit codes:
    0 = the workflow completed
    1 = the workflow failed, was killed, or no terminal status was recorded
    2 = usage error or unreadable stream
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import signal
import sys
from collections import Counter
from pathlib import Path
from typing import Any

TERMINAL_OK = "completed"


def load_stream(stream_file: Path) -> list[dict[str, Any]]:
    if not stream_file.is_file():
        raise FileNotFoundError(f"stream file not found: {stream_file}")
    messages = []
    for number, line in enumerate(stream_file.read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            messages.append(json.loads(line))
        except json.JSONDecodeError as err:
            raise ValueError(f"{stream_file} line {number} is not JSON: {err}") from err
    return messages


def blocks(messages: list[dict[str, Any]], kind: str, role: str):
    for message in messages:
        if message.get("type") == role:
            for block in message.get("message", {}).get("content", []):
                if isinstance(block, dict) and block.get("type") == kind:
                    yield block


def systems(messages: list[dict[str, Any]], subtype: str):
    for message in messages:
        if message.get("type") == "system" and message.get("subtype") == subtype:
            yield message


def transcript_dir(text: str) -> Path | None:
    """The launch acknowledgement names the workflow transcript directory."""
    for line in text.splitlines():
        if line.startswith("Transcript dir: "):
            return Path(line.split(": ", 1)[1].strip())
    return None


def read_task_output(path: str | None) -> dict[str, Any]:
    """The task output file holds the workflow's own return value; it is empty on failure."""
    if not path:
        return {}
    output = Path(path)
    if not output.is_file() or output.stat().st_size == 0:
        return {}
    try:
        return json.loads(output.read_text())
    except json.JSONDecodeError:
        return {}


def journal_counts(journal: Path) -> tuple[list[str], int]:
    if not journal.is_file():
        return [], 0
    started, finished = [], 0
    for line in journal.read_text().splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("type") == "started":
            started.append(entry.get("label") or "")
        elif entry.get("type") == "result":
            finished += 1
    return started, finished


INPUT_PREVIEW = 300
RESULT_PREVIEW = 200


def agent_tool_calls(directory: Path) -> list[dict[str, Any]]:
    """One row per tool call made by each agent of the workflow, read from the agents' transcripts.

    The launcher's stream records only its single `Workflow` call: the agents run inside the workflow runtime and
    write their own `agent-<id>.jsonl` next to the journal. Without this, a run cannot say which tools its agents
    actually used, which made the KB-tool question unanswerable in an earlier run. The input is kept truncated:
    it is an audit trail of what was called, not a second copy of the payload.
    """
    rows: list[dict[str, Any]] = []
    for transcript in sorted(directory.glob("agent-*.jsonl")):
        try:
            results = _tool_results(transcript)
            lines = transcript.read_text().splitlines()
        except OSError as error:
            # A finished run must not be reported as failed because one transcript could not be read: the
            # audit trail is provenance, and an incomplete one is worth more than an aborted extraction.
            print(f"warning: could not read {transcript}: {error}", file=sys.stderr)
            continue
        agent_id = transcript.stem[len("agent-") :]
        meta_file = directory / f"agent-{agent_id}.meta.json"
        meta = {}
        if meta_file.is_file():
            try:
                meta = json.loads(meta_file.read_text())
            except json.JSONDecodeError:
                meta = {}
        ordinal = 0
        for line in lines:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            content = (event.get("message") or {}).get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                ordinal += 1
                rows.append(
                    {
                        "agent_id": agent_id,
                        "label": meta.get("description"),
                        "phase": meta.get("workflowPhase"),
                        "model": meta.get("model"),
                        "ordinal": ordinal,
                        "tool": block.get("name"),
                        "at": event.get("timestamp"),
                        "input_preview": json.dumps(block.get("input"), sort_keys=True)[:INPUT_PREVIEW],
                        **results.get(str(block.get("id")), {"status": "missing"}),
                    }
                )
    return rows


def archive_agent_transcripts(directory: Path, results_dir: Path, stage: str) -> list[dict[str, Any]]:
    """Copy each agent transcript into the run archive, and return one row per file.

    Why this is not redundant with `<stage>-agent-tools.jsonl`. That file is an audit trail: it truncates
    each call's input to 300 characters and its result to 200, and a run's results are mostly longer than
    that — measured on a production run, 219 of 299 recorded results were truncated, the largest being 42 kB.
    So the audit trail says *which* tools an agent called and cannot say *what it was handed*, which is the
    difference between a quote the agent read in a tool result and one it wrote from memory. That question
    is open against this system (the `review` phase's unverifiable quotes) and it cannot be asked of any
    run archived so far: the transcripts live outside the repository, under `~/.claude/projects/`, and the
    harness removes them. Checked 2026-10-08 — of the 73 `transcript_dir` paths recorded across the
    archives, **none** still existed. Hence the copy: the evidence has to be taken while it is there.

    The `.meta.json` beside each transcript carries the agent's label, phase and model, and is copied too.
    An unreadable file is reported and skipped: provenance is best-effort here, and a missing transcript
    must not fail a finished run.
    """
    rows: list[dict[str, Any]] = []
    target_dir = results_dir / "transcripts" / stage
    for source in sorted(directory.glob("agent-*.jsonl")) + sorted(directory.glob("agent-*.meta.json")):
        try:
            data = source.read_bytes()
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / source.name).write_bytes(data)
        except OSError as error:
            print(f"warning: could not archive {source}: {error}", file=sys.stderr)
            continue
        rows.append(
            {
                "agent_id": source.name.split(".")[0][len("agent-") :],
                "file": str((target_dir / source.name).relative_to(results_dir)),
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    return rows


def _tool_results(transcript: Path) -> dict[str, dict[str, Any]]:
    """The outcome of each tool call of one agent, keyed by `tool_use_id`.

    Without this a call's row said only that it was made. Diagnosing the `ToolSearch` loop of
    `probe-tools-20261003-112705` meant reading the runtime's own transcript, which is outside the archive and
    may be deleted, so the diagnosis was not reproducible. `status` is `error` when the runtime marked the
    result as one, `ok` otherwise, and `missing` when no result was recorded (a call the agent never saw answered).
    """
    results: dict[str, dict[str, Any]] = {}
    for line in transcript.read_text().splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        content = (event.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            body = block.get("content")
            text = body if isinstance(body, str) else json.dumps(body, sort_keys=True)
            results[str(block.get("tool_use_id"))] = {
                "status": "error" if block.get("is_error") else "ok",
                "result_bytes": len(text or ""),
                "result_preview": (text or "")[:RESULT_PREVIEW],
            }
    return results


def describe_launcher_exit(exit_code: int | None) -> dict[str, Any] | None:
    """Explain a non-zero launcher exit code: 124 is `timeout`, 128+N is death by signal N.

    A launcher that dies by signal (e.g. 134 = SIGABRT, a runtime crash) leaves no workflow error,
    so the exit code is the only diagnostic; naming the signal makes it visible in summary.json.
    """
    if exit_code is None:
        return None
    info: dict[str, Any] = {"code": exit_code, "signal": None, "meaning": None}
    if exit_code == 124:
        info["meaning"] = "killed by the wall-clock `timeout`"
    elif exit_code > 128:
        try:
            info["signal"] = signal.Signals(exit_code - 128).name
        except ValueError:
            info["signal"] = f"SIG{exit_code - 128}"
        info["meaning"] = f"launcher process terminated by {info['signal']}"
    return info


def extract(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """Build the record of what the workflow actually did."""
    calls = [block for block in blocks(messages, "tool_use", "assistant") if block.get("name") == "Workflow"]
    if not calls:
        return {"status": "not_launched", "error": "the Workflow tool was never called"}

    tool_use_id = calls[0]["id"]
    launch_text = ""
    for block in blocks(messages, "tool_result", "user"):
        if block.get("tool_use_id") == tool_use_id:
            content = block.get("content", "")
            launch_text = content if isinstance(content, str) else json.dumps(content)
            break

    task_ids = {
        message.get("task_id")
        for message in systems(messages, "task_started")
        if message.get("tool_use_id") == tool_use_id
    }
    # Fall back to the notification's own binding when task_started carried no tool_use_id.
    notification = next(
        (
            message
            for message in systems(messages, "task_notification")
            if message.get("task_id") in task_ids or message.get("tool_use_id") == tool_use_id
        ),
        None,
    )
    update = next(
        (
            message
            for message in systems(messages, "task_updated")
            if message.get("task_id") in task_ids
            and isinstance(message.get("patch"), dict)
            and message["patch"].get("status")
        ),
        None,
    )

    directory = transcript_dir(launch_text)
    patch = (update or {}).get("patch", {})
    status = (notification or {}).get("status") or patch.get("status") or "no_terminal_status"
    output = read_task_output((notification or {}).get("output_file"))
    final = next((message for message in messages if message.get("type") == "result"), None)

    return {
        "status": status,
        "error": patch.get("error") or ((notification or {}).get("summary") if status != TERMINAL_OK else None),
        "usage": (notification or {}).get("usage"),
        "cost_usd": (final or {}).get("total_cost_usd"),
        "summary": output.get("summary"),
        "agent_count": output.get("agentCount"),
        "logs": output.get("logs") or [],
        "result": output.get("result"),
        "launch": launch_text,
        "transcript_dir": str(directory or ""),
        # The workflow run id (wf_…) names the runtime's saved agent results; a resume needs it.
        "workflow_run_id": directory.name if directory else None,
    }


def write_evaluator_artifacts(
    results_dir: Path, stream_file: Path, record: dict[str, Any], journal: Path | None, exit_code: int | None
) -> None:
    """Write the summary artifacts of the evaluated stage (read by the observer UI)."""
    if record.get("result") is not None:
        (results_dir / "result.json").write_text(json.dumps(record["result"], indent=2) + "\n")

    started, finished = journal_counts(journal) if journal else ([], 0)
    summary = {
        "workflow_status": record.get("status"),
        "workflow_error": record.get("error"),
        "agents_started": started,
        "agents_finished": finished,
        "agent_count": record.get("agent_count"),
        "cost_usd": record.get("cost_usd"),
        "usage": record.get("usage"),
        "logs": record.get("logs"),
        "launcher_exit": describe_launcher_exit(exit_code),
    }
    (results_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")

    shutil.copyfile(stream_file, results_dir / "stream.jsonl")
    if journal and journal.is_file():
        shutil.copyfile(journal, results_dir / "journal.jsonl")
    if exit_code is not None:
        (results_dir / "exit_code").write_text(f"{exit_code}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stream_file", type=Path)
    parser.add_argument("results_dir", type=Path)
    parser.add_argument("--stage", required=True, help="stage name used to scope the output files")
    parser.add_argument(
        "--evaluated-stage",
        default="discovery",
        help="the stage whose artifacts the evaluator reads (default: discovery)",
    )
    parser.add_argument(
        "--no-archive-transcripts",
        dest="archive_transcripts",
        action="store_false",
        help="do not copy the agents' transcripts into the archive (they are the bulk of its "
        "size; without them a quote cannot be traced to what the agent was handed)",
    )
    parser.add_argument("--exit-code", type=int, default=None, help="the launcher's exit code for this stage")
    parser.add_argument("--started", action="store_true", help="exit 0 if task started, 3 if not (for retry detection)")
    args = parser.parse_args(argv)

    try:
        messages = load_stream(args.stream_file)
    except (FileNotFoundError, ValueError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 2

    # If --started flag is set, just check if task_started events exist and exit accordingly
    if args.started:
        task_started_events = list(systems(messages, "task_started"))
        if task_started_events:
            return 0  # Task started, no retry needed
        else:
            return 3  # Task never started, retry recommended

    record = extract(messages)
    record["launcher_exit"] = describe_launcher_exit(args.exit_code)

    journal = None
    if record.get("transcript_dir"):
        candidate = Path(record["transcript_dir"]) / "journal.jsonl"
        if candidate.is_file():
            journal = candidate
            shutil.copyfile(candidate, args.results_dir / f"{args.stage}-journal.jsonl")
        calls = agent_tool_calls(Path(record["transcript_dir"]))
        if calls:
            (args.results_dir / f"{args.stage}-agent-tools.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in calls)
            )
        # Counts per tool land in the record, so state.json says what the agents used without opening the rows.
        record["agent_tools"] = dict(sorted(Counter(row["tool"] for row in calls).items()))
        if args.archive_transcripts:
            archived = archive_agent_transcripts(Path(record["transcript_dir"]), args.results_dir, args.stage)
            # The size is recorded, not hidden: these files are the bulk of what a deposit would carry.
            record["transcripts"] = {"files": archived, "bytes": sum(row["bytes"] for row in archived)}
        else:
            record["transcripts"] = {"files": [], "bytes": 0, "skipped": "--no-archive-transcripts"}

    (args.results_dir / f"{args.stage}-result.json").write_text(json.dumps(record, indent=2) + "\n")

    if args.stage == args.evaluated_stage:
        write_evaluator_artifacts(args.results_dir, args.stream_file, record, journal, args.exit_code)

    status = record.get("status")
    print(f"{args.stage}: workflow status {status}")
    for line in record.get("logs") or []:
        print(f"  log: {line}")
    if status != TERMINAL_OK:
        print(f"error: {args.stage} workflow did not complete: {record.get('error')}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
