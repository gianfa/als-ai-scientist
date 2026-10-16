"""Tests for scripts/ops/extract_agent_prompts.py (repository root).

The stage streams only carry a truncated `promptPreview` per agent; the full prompt text lives
only in the harness's own per-agent transcripts outside the repository. These tests cover the
root-turn extraction rule (the only "user" turn with no parent and a plain-string content is the
genuine task prompt; every later "user" turn is a tool_result list) and the failure modes this
best-effort archival step must survive without ever raising.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_module():
    path = REPO_ROOT / "scripts" / "ops" / "extract_agent_prompts.py"
    spec = importlib.util.spec_from_file_location("extract_agent_prompts", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


extract_agent_prompts = _load_module()


def root_turn(text: str, timestamp: str) -> dict:
    return {
        "type": "user",
        "parentUuid": None,
        "timestamp": timestamp,
        "message": {"role": "user", "content": text},
    }


def tool_result_turn(timestamp: str) -> dict:
    return {
        "type": "user",
        "parentUuid": "some-uuid",
        "timestamp": timestamp,
        "message": {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]},
    }


def write_agent(transcript_dir: Path, agent_id: str, entries: list[dict], meta: dict | None = None):
    (transcript_dir / f"agent-{agent_id}.jsonl").write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    if meta is not None:
        (transcript_dir / f"agent-{agent_id}.meta.json").write_text(json.dumps(meta))


def make_transcript_dir(tmp_path: Path, session_id: str = "sess1", run_id: str = "wf_1") -> Path:
    transcript_dir = tmp_path / "home" / session_id / "subagents" / "workflows" / run_id
    transcript_dir.mkdir(parents=True)
    return transcript_dir


def write_stage_result(results_dir: Path, stage: str, transcript_dir: Path):
    (results_dir / f"{stage}-result.json").write_text(
        json.dumps({"status": "completed", "transcript_dir": str(transcript_dir)})
    )


def test_extracts_root_prompt_with_agent_id_timestamp_and_to_whom(tmp_path):
    results_dir = tmp_path / "results"
    results_dir.mkdir()
    transcript_dir = make_transcript_dir(tmp_path)
    write_agent(
        transcript_dir,
        "abc123",
        [root_turn("do the thing", "2026-09-29T11:00:00.000Z"), tool_result_turn("2026-09-29T11:00:05.000Z")],
        meta={
            "agentType": "als-evidence",
            "description": "evidence-1",
            "workflowPhase": "Evidence",
            "model": "claude-haiku-4-5-20251001",
            "spawnDepth": 1,
        },
    )
    write_stage_result(results_dir, "discovery", transcript_dir)

    code = extract_agent_prompts.main([str(results_dir), "--run-id", "e2e-test"])

    assert code == 0
    records = [json.loads(l) for l in (results_dir / "prompts.jsonl").read_text().splitlines()]
    assert len(records) == 1
    r = records[0]
    assert r["run_id"] == "e2e-test"
    assert r["stage"] == "discovery"
    assert r["agent_id"] == "abc123"
    assert r["agent_type"] == "als-evidence"
    assert r["label"] == "evidence-1"
    assert r["timestamp"] == "2026-09-29T11:00:00.000Z"
    assert r["prompt"] == "do the thing"
    assert r["prompt_sha256"] == hashlib.sha256(b"do the thing").hexdigest()
    assert r["to_whom"] == "agent:abc123"
    assert r["produced_by"] == f"workflow:{transcript_dir.name}"
    assert r["workflow_run_id"] == transcript_dir.name
    assert r["session_id"] == "sess1"


def test_tool_result_turns_are_not_mistaken_for_prompts(tmp_path):
    results_dir = tmp_path / "results"
    results_dir.mkdir()
    transcript_dir = make_transcript_dir(tmp_path)
    write_agent(
        transcript_dir,
        "abc123",
        [
            root_turn("the only real prompt", "2026-09-29T11:00:00.000Z"),
            tool_result_turn("2026-09-29T11:00:05.000Z"),
            tool_result_turn("2026-09-29T11:00:10.000Z"),
        ],
        meta={"agentType": "als-evidence"},
    )
    write_stage_result(results_dir, "discovery", transcript_dir)

    extract_agent_prompts.main([str(results_dir), "--run-id", "e2e-test"])

    records = [json.loads(l) for l in (results_dir / "prompts.jsonl").read_text().splitlines()]
    assert len(records) == 1
    assert records[0]["prompt"] == "the only real prompt"


def test_a_retried_agent_slot_yields_one_record_per_attempt(tmp_path):
    results_dir = tmp_path / "results"
    results_dir.mkdir()
    transcript_dir = make_transcript_dir(tmp_path)
    write_agent(
        transcript_dir,
        "abc123",
        [
            root_turn("first attempt", "2026-09-29T11:00:00.000Z"),
            tool_result_turn("2026-09-29T11:00:05.000Z"),
            root_turn("retry after restart", "2026-09-29T11:01:00.000Z"),
        ],
        meta={"agentType": "als-evidence"},
    )
    write_stage_result(results_dir, "discovery", transcript_dir)

    extract_agent_prompts.main([str(results_dir), "--run-id", "e2e-test"])

    records = [json.loads(l) for l in (results_dir / "prompts.jsonl").read_text().splitlines()]
    assert len(records) == 2
    assert [r["attempt"] for r in records] == [1, 2]
    assert [r["prompt"] for r in records] == ["first attempt", "retry after restart"]


def test_a_stage_that_never_ran_is_skipped_without_error(tmp_path):
    results_dir = tmp_path / "results"
    results_dir.mkdir()
    # No ca-result.json, no discovery-result.json at all.

    code = extract_agent_prompts.main([str(results_dir), "--run-id", "e2e-test"])

    assert code == 0
    assert (results_dir / "prompts.jsonl").read_text() == ""


def test_a_stage_result_missing_transcript_dir_is_skipped_without_error(tmp_path):
    results_dir = tmp_path / "results"
    results_dir.mkdir()
    (results_dir / "discovery-result.json").write_text(json.dumps({"status": "completed"}))

    code = extract_agent_prompts.main([str(results_dir), "--run-id", "e2e-test"])

    assert code == 0
    assert (results_dir / "prompts.jsonl").read_text() == ""


def test_a_transcript_dir_absent_on_this_machine_is_skipped_without_error(tmp_path):
    results_dir = tmp_path / "results"
    results_dir.mkdir()
    write_stage_result(results_dir, "discovery", tmp_path / "gone" / "wf_x")

    code = extract_agent_prompts.main([str(results_dir), "--run-id", "e2e-test"])

    assert code == 0
    assert (results_dir / "prompts.jsonl").read_text() == ""


def test_a_malformed_stage_result_json_is_treated_as_absent(tmp_path):
    results_dir = tmp_path / "results"
    results_dir.mkdir()
    (results_dir / "discovery-result.json").write_text("{not json")

    code = extract_agent_prompts.main([str(results_dir), "--run-id", "e2e-test"])

    assert code == 0
    assert (results_dir / "prompts.jsonl").read_text() == ""


def test_a_malformed_jsonl_line_is_warned_and_skipped(tmp_path, capsys):
    results_dir = tmp_path / "results"
    results_dir.mkdir()
    transcript_dir = make_transcript_dir(tmp_path)
    (transcript_dir / "agent-abc123.jsonl").write_text(
        json.dumps(root_turn("valid prompt", "2026-09-29T11:00:00.000Z")) + "\n{not json\n"
    )
    write_stage_result(results_dir, "discovery", transcript_dir)

    code = extract_agent_prompts.main([str(results_dir), "--run-id", "e2e-test"])

    assert code == 0
    records = [json.loads(l) for l in (results_dir / "prompts.jsonl").read_text().splitlines()]
    assert len(records) == 1
    assert records[0]["prompt"] == "valid prompt"
    assert "not JSON" in capsys.readouterr().err


def test_an_agent_transcript_with_no_root_turn_is_warned_and_skipped(tmp_path, capsys):
    results_dir = tmp_path / "results"
    results_dir.mkdir()
    transcript_dir = make_transcript_dir(tmp_path)
    write_agent(transcript_dir, "abc123", [tool_result_turn("2026-09-29T11:00:05.000Z")])
    write_stage_result(results_dir, "discovery", transcript_dir)

    code = extract_agent_prompts.main([str(results_dir), "--run-id", "e2e-test"])

    assert code == 0
    assert (results_dir / "prompts.jsonl").read_text() == ""
    assert "no root prompt turn" in capsys.readouterr().err


def test_missing_meta_json_does_not_crash_and_leaves_fields_none(tmp_path):
    results_dir = tmp_path / "results"
    results_dir.mkdir()
    transcript_dir = make_transcript_dir(tmp_path)
    write_agent(transcript_dir, "abc123", [root_turn("no meta here", "2026-09-29T11:00:00.000Z")])
    write_stage_result(results_dir, "discovery", transcript_dir)

    code = extract_agent_prompts.main([str(results_dir), "--run-id", "e2e-test"])

    assert code == 0
    records = [json.loads(l) for l in (results_dir / "prompts.jsonl").read_text().splitlines()]
    assert len(records) == 1
    assert records[0]["agent_type"] is None
    assert records[0]["label"] is None


def test_records_from_both_stages_are_sorted_by_timestamp(tmp_path):
    results_dir = tmp_path / "results"
    results_dir.mkdir()
    ca_dir = make_transcript_dir(tmp_path, session_id="sess1", run_id="wf_ca")
    discovery_dir = make_transcript_dir(tmp_path, session_id="sess1", run_id="wf_discovery")
    write_agent(ca_dir, "aaa", [root_turn("ca prompt", "2026-09-29T11:05:00.000Z")])
    write_agent(discovery_dir, "bbb", [root_turn("discovery prompt", "2026-09-29T11:00:00.000Z")])
    write_stage_result(results_dir, "ca", ca_dir)
    write_stage_result(results_dir, "discovery", discovery_dir)

    extract_agent_prompts.main([str(results_dir), "--run-id", "e2e-test"])

    records = [json.loads(l) for l in (results_dir / "prompts.jsonl").read_text().splitlines()]
    assert [r["stage"] for r in records] == ["discovery", "ca"]


def test_run_id_defaults_to_results_dir_parent_name(tmp_path):
    run_dir = tmp_path / "e2e-042"
    results_dir = run_dir / "results"
    results_dir.mkdir(parents=True)
    transcript_dir = make_transcript_dir(tmp_path)
    write_agent(transcript_dir, "abc123", [root_turn("hi", "2026-09-29T11:00:00.000Z")])
    write_stage_result(results_dir, "discovery", transcript_dir)

    extract_agent_prompts.main([str(results_dir)])

    records = [json.loads(l) for l in (results_dir / "prompts.jsonl").read_text().splitlines()]
    assert records[0]["run_id"] == "e2e-042"


def test_exit_code_is_always_zero_even_with_only_warnings(tmp_path):
    results_dir = tmp_path / "results"
    results_dir.mkdir()
    (results_dir / "discovery-result.json").write_text("{not json")

    assert extract_agent_prompts.main([str(results_dir)]) == 0
