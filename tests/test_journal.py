"""Tests for observability/wf_observer/journal.py.

Workflow journal lines carry fields that `Event.from_dict` rejects with `InvalidEventError`. Fed to
it, every line of a `run.sh` journal would be discarded in silence and `GET /runs/<id>/events` would
return an empty list while appearing to work.
"""

from __future__ import annotations

import json
from pathlib import Path

from observability.wf_observer.journal import workflow_journal_events


def write_journal(tmp_path: Path, *entries: dict) -> Path:
    path = tmp_path / "journal.jsonl"
    path.write_text("".join(json.dumps(entry) + "\n" for entry in entries))
    return path


def test_a_workflow_journal_yields_events_instead_of_nothing(tmp_path):
    path = write_journal(
        tmp_path,
        {"type": "launched"},
        {"type": "started", "agentId": "a1", "label": "evidence-1", "phase": "Evidence"},
        {"type": "result", "agentId": "a1", "result": "ALPHA"},
    )

    events = workflow_journal_events(path)

    assert [e["kind"] for e in events] == ["run_started", "agent_started", "agent_finished"]
    assert events[1]["agent"] == "evidence-1"
    assert events[1]["phase"] == "Evidence"


def test_a_result_inherits_the_label_and_phase_of_its_started_line(tmp_path):
    """A `result` line carries only an agentId; without the join it reads as an anonymous event."""
    path = write_journal(
        tmp_path,
        {"type": "started", "agentId": "a1", "label": "critic-H1", "phase": "Critique"},
        {"type": "result", "agentId": "a1", "result": {"verdict": "rejected"}},
    )

    finished = workflow_journal_events(path)[-1]

    assert finished["agent"] == "critic-H1"
    assert finished["phase"] == "Critique"
    assert "rejected" in finished["summary"]


def test_a_long_result_is_summarised_rather_than_returned_whole(tmp_path):
    path = write_journal(tmp_path, {"type": "result", "agentId": "a1", "result": "x" * 5000})

    summary = workflow_journal_events(path)[0]["summary"]

    assert len(summary) < 400 and summary.endswith("…")


def test_a_truncated_last_line_is_skipped_not_fatal(tmp_path):
    """A live run is appended to while it is read, so a half-written line is normal."""
    path = tmp_path / "journal.jsonl"
    path.write_text(json.dumps({"type": "launched"}) + "\n" + '{"type": "start')

    assert [e["kind"] for e in workflow_journal_events(path)] == ["run_started"]


def test_an_unrecognised_type_is_kept_rather_than_dropped(tmp_path):
    """Silently dropping unknown runtime events is how the original defect stayed invisible."""
    path = write_journal(tmp_path, {"type": "checkpoint", "agentId": "a1"})

    assert [e["kind"] for e in workflow_journal_events(path)] == ["checkpoint"]


def test_a_missing_journal_is_empty_not_an_error(tmp_path):
    assert workflow_journal_events(tmp_path / "absent.jsonl") == []


def test_the_real_e2e_015_journal_is_readable(tmp_path):
    """A real archived journal, when present in the checkout; skipped otherwise."""
    path = Path(__file__).resolve().parents[1] / "experiments" / "e2e-015" / "results" / "discovery-journal.jsonl"
    if not path.is_file():
        return

    events = workflow_journal_events(path)

    assert len(events) > 1
    assert {"evidence-1", "evidence-checker"} <= {e["agent"] for e in events if e["agent"]}


def test_a_causal_graph_result_is_carried_whole_for_rendering(tmp_path):
    graph = {"nodes": [{"id": "N1", "label": "x" * 500, "kind": "gene"}], "edges": []}
    path = write_journal(tmp_path, {"type": "result", "agentId": "a1", "result": graph})

    event = workflow_journal_events(path)[0]

    assert event["causal_graph"] == graph
    assert event["summary"].endswith("…")


def test_a_non_graph_result_carries_no_causal_graph(tmp_path):
    path = write_journal(tmp_path, {"type": "result", "agentId": "a1", "result": {"verdict": "ok"}})

    assert "causal_graph" not in workflow_journal_events(path)[0]
