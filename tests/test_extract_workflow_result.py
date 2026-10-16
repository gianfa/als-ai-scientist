"""Tests for pipeline/extract_workflow_result.py (repository root).

The Workflow tool result is only a launch acknowledgement, so the extractor must read the
background-task events. Reporting a failed workflow as a success is the defect these cover.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TOOL_USE_ID = "toolu_1"
TASK_ID = "task_1"


def _load_module():
    path = REPO_ROOT / "pipeline" / "extract_workflow_result.py"
    spec = importlib.util.spec_from_file_location("extract_workflow_result", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


extract_workflow_result = _load_module()


def launch_text(transcript: Path) -> str:
    return "Workflow launched in background. Task ID: task_1\n" f"Transcript dir: {transcript}\n" "Run ID: wf_test\n"


def stream(tmp_path: Path, status: str, output_file: Path | None, error: str | None, transcript: Path) -> Path:
    messages = [
        {
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "id": TOOL_USE_ID, "name": "Workflow", "input": {}}]},
        },
        {
            "type": "user",
            "message": {
                "content": [{"type": "tool_result", "tool_use_id": TOOL_USE_ID, "content": launch_text(transcript)}]
            },
        },
        {"type": "system", "subtype": "task_started", "task_id": TASK_ID, "tool_use_id": TOOL_USE_ID},
        {
            "type": "system",
            "subtype": "task_updated",
            "task_id": TASK_ID,
            "patch": {"status": status, **({"error": error} if error else {})},
        },
        {
            "type": "system",
            "subtype": "task_notification",
            "task_id": TASK_ID,
            "status": status,
            "output_file": str(output_file) if output_file else None,
            "usage": {"total_tokens": 10, "duration_ms": 5},
        },
        {"type": "result", "subtype": "success", "total_cost_usd": 0.25},
    ]
    path = tmp_path / f"{status}-stream.jsonl"
    path.write_text("\n".join(json.dumps(m) for m in messages) + "\n")
    return path


def transcript_with_journal(tmp_path: Path) -> Path:
    transcript = tmp_path / "transcript"
    transcript.mkdir()
    (transcript / "journal.jsonl").write_text(
        json.dumps({"type": "started", "label": "evidence-1"}) + "\n" + json.dumps({"type": "result"}) + "\n"
    )
    return transcript


def test_a_failed_workflow_is_not_reported_as_success(tmp_path):
    results = tmp_path / "results"
    results.mkdir()
    transcript = transcript_with_journal(tmp_path)
    path = stream(tmp_path, "failed", None, "Error: agent type 'x' not found", transcript)

    code = extract_workflow_result.main([str(path), str(results), "--stage", "discovery"])

    assert code == 1
    record = json.loads((results / "discovery-result.json").read_text())
    assert record["status"] == "failed"
    assert "agent type 'x' not found" in record["error"]


def test_a_completed_workflow_yields_its_own_result(tmp_path):
    results = tmp_path / "results"
    results.mkdir()
    transcript = transcript_with_journal(tmp_path)
    output_file = tmp_path / "task.output"
    output_file.write_text(
        json.dumps(
            {
                "summary": "s",
                "agentCount": 1,
                "logs": ["one log"],
                "result": {"config_hash": "a" * 64, "outcome": "proposal"},
            }
        )
    )
    path = stream(tmp_path, "completed", output_file, None, transcript)

    code = extract_workflow_result.main([str(path), str(results), "--stage", "discovery", "--exit-code", "0"])

    assert code == 0
    # The workflow's own return value, not the launch acknowledgement.
    assert json.loads((results / "result.json").read_text())["outcome"] == "proposal"
    summary = json.loads((results / "summary.json").read_text())
    assert summary["workflow_status"] == "completed"
    assert summary["workflow_error"] is None
    assert summary["agents_started"] == ["evidence-1"]
    assert summary["agents_finished"] == 1
    assert summary["cost_usd"] == 0.25
    # The summary artifacts the observer UI reads.
    for name in ("result.json", "summary.json", "stream.jsonl", "journal.jsonl", "exit_code"):
        assert (results / name).is_file(), name
    assert (results / "exit_code").read_text().strip() == "0"


def test_a_stage_that_is_not_evaluated_does_not_write_evaluator_artifacts(tmp_path):
    results = tmp_path / "results"
    results.mkdir()
    transcript = transcript_with_journal(tmp_path)
    output_file = tmp_path / "task.output"
    output_file.write_text(json.dumps({"summary": "s", "agentCount": 1, "logs": [], "result": {"ok": True}}))
    path = stream(tmp_path, "completed", output_file, None, transcript)

    code = extract_workflow_result.main([str(path), str(results), "--stage", "ca"])

    assert code == 0
    assert (results / "ca-result.json").is_file()
    # The discovery stage owns result.json; the CA stage must not overwrite it.
    assert not (results / "result.json").exists()
    assert not (results / "summary.json").exists()


def test_a_missing_terminal_status_is_a_failure(tmp_path):
    results = tmp_path / "results"
    results.mkdir()
    transcript = transcript_with_journal(tmp_path)
    messages = [
        {
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "id": TOOL_USE_ID, "name": "Workflow", "input": {}}]},
        },
        {
            "type": "user",
            "message": {
                "content": [{"type": "tool_result", "tool_use_id": TOOL_USE_ID, "content": launch_text(transcript)}]
            },
        },
        {"type": "system", "subtype": "task_started", "task_id": TASK_ID, "tool_use_id": TOOL_USE_ID},
    ]
    path = tmp_path / "cut.jsonl"
    path.write_text("\n".join(json.dumps(m) for m in messages) + "\n")

    code = extract_workflow_result.main([str(path), str(results), "--stage", "discovery"])

    assert code == 1
    assert json.loads((results / "discovery-result.json").read_text())["status"] == "no_terminal_status"


def test_a_stream_without_a_workflow_call_is_a_failure(tmp_path):
    results = tmp_path / "results"
    results.mkdir()
    path = tmp_path / "empty.jsonl"
    path.write_text(json.dumps({"type": "result", "subtype": "success"}) + "\n")

    code = extract_workflow_result.main([str(path), str(results), "--stage", "discovery"])

    assert code == 1
    assert json.loads((results / "discovery-result.json").read_text())["status"] == "not_launched"


def test_an_unreadable_stream_is_a_usage_error(tmp_path):
    results = tmp_path / "results"
    results.mkdir()
    path = tmp_path / "bad.jsonl"
    path.write_text("{not json\n")

    assert extract_workflow_result.main([str(path), str(results), "--stage", "discovery"]) == 2
    assert extract_workflow_result.main([str(tmp_path / "nope.jsonl"), str(results), "--stage", "x"]) == 2


def test_started_flag_returns_zero_when_task_started(tmp_path):
    results = tmp_path / "results"
    results.mkdir()
    transcript = transcript_with_journal(tmp_path)
    path = stream(tmp_path, "completed", None, None, transcript)

    code = extract_workflow_result.main([str(path), str(results), "--stage", "discovery", "--started"])

    assert code == 0


def test_started_flag_returns_three_when_task_not_started(tmp_path):
    results = tmp_path / "results"
    results.mkdir()
    transcript = transcript_with_journal(tmp_path)
    messages = [
        {
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "id": TOOL_USE_ID, "name": "Workflow", "input": {}}]},
        },
        {
            "type": "user",
            "message": {
                "content": [{"type": "tool_result", "tool_use_id": TOOL_USE_ID, "content": launch_text(transcript)}]
            },
        },
        # No task_started event
    ]
    path = tmp_path / "no_start.jsonl"
    path.write_text("\n".join(json.dumps(m) for m in messages) + "\n")

    code = extract_workflow_result.main([str(path), str(results), "--stage", "discovery", "--started"])

    assert code == 3


@pytest.mark.parametrize("code, signal_name", [(134, "SIGABRT"), (137, "SIGKILL"), (139, "SIGSEGV")])
def test_a_launcher_killed_by_a_signal_is_named_in_the_summary(tmp_path, code, signal_name):
    results = tmp_path / "results"
    results.mkdir()
    path = stream(tmp_path, "failed", None, "boom", transcript_with_journal(tmp_path))

    extract_workflow_result.main([str(path), str(results), "--stage", "discovery", "--exit-code", str(code)])

    summary = json.loads((results / "summary.json").read_text())
    assert summary["launcher_exit"]["code"] == code
    assert summary["launcher_exit"]["signal"] == signal_name
    record = json.loads((results / "discovery-result.json").read_text())
    assert record["launcher_exit"]["signal"] == signal_name


def test_launcher_exit_codes_without_a_signal_have_none():
    assert extract_workflow_result.describe_launcher_exit(None) is None
    assert extract_workflow_result.describe_launcher_exit(0)["signal"] is None
    assert extract_workflow_result.describe_launcher_exit(1)["signal"] is None
    assert "timeout" in extract_workflow_result.describe_launcher_exit(124)["meaning"]


def _agent_transcript(
    transcript: Path, agent_id: str, label: str, tools: list[str], results: dict[int, dict] | None = None
) -> None:
    (transcript / f"agent-{agent_id}.meta.json").write_text(
        json.dumps({"description": label, "workflowPhase": "intervene", "model": "claude-haiku-4-5"})
    )
    lines = []
    for i, tool in enumerate(tools):
        lines.append(
            json.dumps(
                {
                    "type": "assistant",
                    "timestamp": f"2026-10-03T07:2{i}:00Z",
                    "message": {
                        "content": [{"type": "tool_use", "id": f"t{i}", "name": tool, "input": {"query": "x" * 400}}]
                    },
                }
            )
        )
        outcome = (results or {}).get(i)
        if outcome is not None:
            lines.append(
                json.dumps(
                    {
                        "type": "user",
                        "message": {"content": [{"type": "tool_result", "tool_use_id": f"t{i}", **outcome}]},
                    }
                )
            )
    lines.append(json.dumps({"type": "user", "message": {"content": "no tool here"}}))
    (transcript / f"agent-{agent_id}.jsonl").write_text("\n".join(lines) + "\n")


def test_the_tool_calls_of_each_agent_are_persisted_and_counted(tmp_path):
    """The launcher's stream holds only its own Workflow call: without the agents' transcripts a run cannot say
    which tools its agents used."""
    results = tmp_path / "results"
    results.mkdir()
    transcript = transcript_with_journal(tmp_path)
    _agent_transcript(transcript, "a1", "intervener@H-9", ["mcp__kb__kb_search", "mcp__kb__kb_search"])
    _agent_transcript(transcript, "a2", "intervener@H-2", ["WebSearch"])
    output_file = tmp_path / "task.output"
    output_file.write_text(json.dumps({"summary": "s", "agentCount": 2, "logs": [], "result": {"ok": True}}))
    path = stream(tmp_path, "completed", output_file, None, transcript)

    assert extract_workflow_result.main([str(path), str(results), "--stage", "07-intervene"]) == 0

    rows = [json.loads(l) for l in (results / "07-intervene-agent-tools.jsonl").read_text().splitlines()]
    assert [(r["agent_id"], r["label"], r["ordinal"], r["tool"]) for r in rows] == [
        ("a1", "intervener@H-9", 1, "mcp__kb__kb_search"),
        ("a1", "intervener@H-9", 2, "mcp__kb__kb_search"),
        ("a2", "intervener@H-2", 1, "WebSearch"),
    ]
    assert rows[0]["phase"] == "intervene" and rows[0]["at"] == "2026-10-03T07:20:00Z"
    assert len(rows[0]["input_preview"]) == extract_workflow_result.INPUT_PREVIEW  # audit trail, not a copy
    record = json.loads((results / "07-intervene-result.json").read_text())
    assert record["agent_tools"] == {"WebSearch": 1, "mcp__kb__kb_search": 2}


def test_a_workflow_whose_agents_called_nothing_writes_no_tool_file(tmp_path):
    results = tmp_path / "results"
    results.mkdir()
    transcript = transcript_with_journal(tmp_path)
    output_file = tmp_path / "task.output"
    output_file.write_text(json.dumps({"summary": "s", "agentCount": 0, "logs": [], "result": {"ok": True}}))
    path = stream(tmp_path, "completed", output_file, None, transcript)

    assert extract_workflow_result.main([str(path), str(results), "--stage", "01-literature"]) == 0

    assert not (results / "01-literature-agent-tools.jsonl").exists()
    assert json.loads((results / "01-literature-result.json").read_text())["agent_tools"] == {}


def test_each_call_carries_its_outcome(tmp_path):
    """Without the outcome a row says only that a call was made, and a failing loop of calls could be
    diagnosed only from the runtime's own transcript, outside the archive."""
    results = tmp_path / "results"
    results.mkdir()
    transcript = transcript_with_journal(tmp_path)
    _agent_transcript(
        transcript,
        "a1",
        "author-1",
        ["mcp__kb__kb_search", "WebFetch", "ToolSearch"],
        results={
            0: {"content": '{"records":[]}'},
            1: {"content": "boom", "is_error": True},
        },
    )
    output_file = tmp_path / "task.output"
    output_file.write_text(json.dumps({"summary": "s", "agentCount": 1, "logs": [], "result": {"ok": True}}))
    path = stream(tmp_path, "completed", output_file, None, transcript)

    assert extract_workflow_result.main([str(path), str(results), "--stage", "02-generate"]) == 0

    rows = [json.loads(l) for l in (results / "02-generate-agent-tools.jsonl").read_text().splitlines()]
    assert [(r["tool"], r["status"]) for r in rows] == [
        ("mcp__kb__kb_search", "ok"),
        ("WebFetch", "error"),
        ("ToolSearch", "missing"),
    ]
    assert rows[0]["result_bytes"] == len('{"records":[]}') and rows[0]["result_preview"] == '{"records":[]}'
    assert "result_bytes" not in rows[2]  # a call the agent never saw answered


def test_the_agents_transcripts_are_copied_into_the_archive(tmp_path):
    """The audit trail truncates each result; the transcript is the only record of what an agent was handed.

    It lives under `~/.claude/projects/`, which the harness clears: of the 73 transcript directories recorded
    across this repository's archives, none still existed when checked on 2026-10-08. So the copy has to
    happen while the run is being extracted, or the evidence is gone for good.
    """
    results = tmp_path / "results"
    results.mkdir()
    transcript = transcript_with_journal(tmp_path)
    _agent_transcript(
        transcript, "a1", "reviewer@H-9", ["mcp__literature__check_quote"], results={0: {"content": "y" * 5000}}
    )
    output_file = tmp_path / "task.output"
    output_file.write_text(json.dumps({"summary": "s", "agentCount": 1, "logs": [], "result": {"ok": True}}))
    path = stream(tmp_path, "completed", output_file, None, transcript)

    assert extract_workflow_result.main([str(path), str(results), "--stage", "05-review"]) == 0

    archived = results / "transcripts" / "05-review" / "agent-a1.jsonl"
    source = transcript / "agent-a1.jsonl"
    assert archived.read_bytes() == source.read_bytes()
    assert (results / "transcripts" / "05-review" / "agent-a1.meta.json").is_file()

    # The full result survives the copy, while the audit trail keeps 200 characters of it.
    assert "y" * 5000 in archived.read_text()
    row = json.loads((results / "05-review-agent-tools.jsonl").read_text().splitlines()[0])
    assert len(row["result_preview"]) == extract_workflow_result.RESULT_PREVIEW

    record = json.loads((results / "05-review-result.json").read_text())
    files = {entry["file"]: entry for entry in record["transcripts"]["files"]}
    entry = files["transcripts/05-review/agent-a1.jsonl"]
    assert entry["agent_id"] == "a1"
    assert entry["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert entry["bytes"] == source.stat().st_size
    assert record["transcripts"]["bytes"] == sum(e["bytes"] for e in files.values())


def test_the_archiving_can_be_declined_and_says_so(tmp_path):
    """The transcripts are the bulk of an archive's size, so the cost has to be refusable — and visible."""
    results = tmp_path / "results"
    results.mkdir()
    transcript = transcript_with_journal(tmp_path)
    _agent_transcript(transcript, "a1", "reviewer@H-9", ["WebSearch"])
    output_file = tmp_path / "task.output"
    output_file.write_text(json.dumps({"summary": "s", "agentCount": 1, "logs": [], "result": {"ok": True}}))
    path = stream(tmp_path, "completed", output_file, None, transcript)

    assert (
        extract_workflow_result.main([str(path), str(results), "--stage", "05-review", "--no-archive-transcripts"]) == 0
    )

    assert not (results / "transcripts").exists()
    record = json.loads((results / "05-review-result.json").read_text())
    assert record["transcripts"] == {"files": [], "bytes": 0, "skipped": "--no-archive-transcripts"}


def test_an_unreadable_transcript_is_reported_and_does_not_fail_the_run(tmp_path, capsys):
    """A finished run must not be reported as failed because one transcript could not be read.

    Before the archiving existed, an unreadable `agent-*.jsonl` raised out of `agent_tool_calls` and the
    extractor died, so a completed, paid run was reported as a failure. The other agents' rows must still
    come out.
    """
    results = tmp_path / "results"
    results.mkdir()
    transcript = transcript_with_journal(tmp_path)
    _agent_transcript(transcript, "a1", "reviewer@H-9", ["WebSearch"])
    (transcript / "agent-a2.jsonl").mkdir()  # a directory where a transcript is expected
    output_file = tmp_path / "task.output"
    output_file.write_text(json.dumps({"summary": "s", "agentCount": 2, "logs": [], "result": {"ok": True}}))
    path = stream(tmp_path, "completed", output_file, None, transcript)

    assert extract_workflow_result.main([str(path), str(results), "--stage", "05-review"]) == 0

    err = capsys.readouterr().err
    assert "could not read" in err and "could not archive" in err
    rows = [json.loads(l) for l in (results / "05-review-agent-tools.jsonl").read_text().splitlines()]
    assert [row["agent_id"] for row in rows] == ["a1"]  # the readable agent is still audited
    record = json.loads((results / "05-review-result.json").read_text())
    assert [entry["agent_id"] for entry in record["transcripts"]["files"]] == ["a1", "a1"]
