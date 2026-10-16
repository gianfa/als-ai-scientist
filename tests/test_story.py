"""Story tab data: prompts (timestamped) joined to journal replies by agentId."""

from __future__ import annotations

import json
from pathlib import Path

from observability.wf_observer.story import TEXT_LIMIT, narrate, run_exchanges, run_story


def write(path: Path, *rows: dict) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def prompt(agent_id: str, ts: str, **extra):
    return {
        "agent_id": agent_id,
        "timestamp": ts,
        "label": f"agent-{agent_id}",
        "phase": "P",
        "produced_by": "workflow:wf",
        "prompt": "do it",
        **extra,
    }


def test_exchanges_are_ordered_by_prompt_time_and_joined_to_replies(tmp_path):
    write(tmp_path / "prompts.jsonl", prompt("b", "2026-01-01T00:00:02Z"), prompt("a", "2026-01-01T00:00:01Z"))
    write(
        tmp_path / "discovery-journal.jsonl",
        {"type": "started", "agentId": "a", "label": "agent-a", "phase": "P"},
        {"type": "result", "agentId": "a", "result": {"verdict": "ok"}},
    )

    exchanges = run_exchanges(tmp_path)

    assert [e["agent_id"] for e in exchanges] == ["a", "b"]
    assert '"verdict": "ok"' in exchanges[0]["reply"]
    assert exchanges[1]["reply"] is None


def test_an_agent_without_a_prompt_record_has_no_timestamp(tmp_path):
    write(
        tmp_path / "journal.jsonl",
        {"type": "started", "agentId": "x", "label": "lonely", "phase": "P"},
        {"type": "result", "agentId": "x", "result": "R"},
    )

    (only,) = run_exchanges(tmp_path)

    assert only["timestamp"] is None and only["to"] == "lonely" and only["reply"] == "R"


def test_long_text_is_cut_and_flagged(tmp_path):
    write(tmp_path / "prompts.jsonl", prompt("a", "2026-01-01T00:00:01Z", prompt="x" * (TEXT_LIMIT + 1)))

    (only,) = run_exchanges(tmp_path)

    assert len(only["prompt"]) == TEXT_LIMIT and only["prompt_truncated"] is True


def test_malformed_lines_and_missing_files_are_tolerated(tmp_path):
    assert run_exchanges(tmp_path) == []
    (tmp_path / "prompts.jsonl").write_text(json.dumps(prompt("a", "2026-01-01T00:00:01Z")) + '\n{"agent_')

    assert len(run_exchanges(tmp_path)) == 1


def test_narrative_is_built_from_replies_and_skips_absent_steps(tmp_path):
    write(
        tmp_path / "discovery-journal.jsonl",
        {"type": "started", "agentId": "h", "label": "hypothesis-generator", "phase": "Hypotheses"},
        {"type": "result", "agentId": "h", "result": {"hypotheses": [{"id": "H1"}, {"id": "H2"}]}},
        {"type": "started", "agentId": "c", "label": "critic-H1", "phase": "Critique"},
        {
            "type": "result",
            "agentId": "c",
            "result": {"hypothesis_id": "H1", "verdict": "rejected", "objections": [{}]},
        },
    )

    text = " ".join(narrate(tmp_path))

    assert "2 competing hypotheses: H1, H2" in text
    assert "H1 rejected (1 objections)" in text
    assert "evidence" not in text.lower()  # no Evidence phase recorded, so no sentence about it


def test_verdict_survives_reply_truncation(tmp_path):
    write(tmp_path / "prompts.jsonl", prompt("a", "2026-01-01T00:00:01Z"))
    write(
        tmp_path / "discovery-journal.jsonl",
        {"type": "result", "agentId": "a", "result": {"verdict": "rejected", "objections": ["x" * (TEXT_LIMIT * 2)]}},
    )

    (only,) = run_exchanges(tmp_path)

    assert only["reply_truncated"] is True and only["verdict"] == "rejected"


def test_workflow_log_lines_become_notes_after_their_agent_and_a_closing_outcome(tmp_path):
    write(tmp_path / "prompts.jsonl", prompt("r", "2026-01-01T00:00:01Z", label="revision-H3"))
    (tmp_path / "discovery-result.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "logs": [
                    "[seq 1] Agent started: revision-H3",
                    "[seq 2] Agent completed: revision-H3",
                    "Revision rejected: H3: objections answered more than once",
                ],
                "result": {
                    "outcome": "insufficient_evidence",
                    "reason": "none survived",
                    "revision_summary": {"rounds_run": 1},
                    "interventions": [],
                    "experiments": [],
                },
            }
        )
    )

    story = run_story(tmp_path)

    (note,) = story["exchanges"][0]["notes"]
    assert note["headline"] == "Revision H3 rejected" and note["details"] == ["objections answered more than once"]
    assert story["closing"][0]["headline"] == "Run ended: insufficient evidence — none survived"
    assert story["closing"][0]["tone"] == "warn"
