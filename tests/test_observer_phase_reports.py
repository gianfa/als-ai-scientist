"""Story, Tokens and the run selector on phase-pipeline runs, read from `NN-<phase>-journal.jsonl` and
`NN-<phase>-report.json`. Synthetic archives only: the real ones under experiments/ are git-ignored."""

from __future__ import annotations

import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from observability.wf_observer.phase_reports import phase_usages, run_totals
from observability.wf_observer.server import make_handler
from observability.wf_observer.store import RunStore
from observability.wf_observer.story import run_story

PHASES = ["data", "generate", "review"]


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data))


def write_jsonl(path: Path, *rows: dict[str, Any]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def launch(stage: str, cost: float, workflow_tokens: int | None, launcher_out: int) -> dict[str, Any]:
    return {
        "stage": stage,
        "wall_s": 60.0,
        "session_cost_usd": cost,
        "launcher_usage": {
            "input_tokens": 1,
            "cache_creation_input_tokens": 10,
            "cache_read_input_tokens": 100,
            "output_tokens": launcher_out,
        },
        "agent_count": 2,
        "workflow_status": "completed",
        "workflow_usage": {"total_tokens": workflow_tokens} if workflow_tokens is not None else None,
    }


def journal(path: Path, phase: str, *agents: tuple[str, str, dict[str, Any] | None]) -> None:
    rows: list[dict[str, Any]] = [{"type": "launched"}]
    rows += [{"type": "started", "agentId": a, "label": label, "phase": phase} for a, label, _ in agents]
    rows += [{"type": "result", "agentId": a, "result": reply} for a, _, reply in agents if reply is not None]
    write_jsonl(path, *rows)


def pipeline_archive(root: Path, run_id: str, replay_of: str | None = None) -> Path:
    """data (no launch), generate (one attempt), review (attempt 2 after a stopped attempt 1)."""
    results = root / run_id / "results"
    results.mkdir(parents=True)
    write_json(results / "context.json", {"run_id": run_id, "phases": PHASES})
    state: dict[str, Any] = {"run_id": run_id, "completed": PHASES, "phases": {}}
    if replay_of:
        state["replay_of"] = {"run_id": "pl-orig", "phase": replay_of}
    write_json(results / "state.json", state)
    write_json(results / "01-data-report.json", {"sources": {}, "absent": []})
    write_json(results / "02-generate-report.json", {"launch": launch("02-generate", 1.0, 1000, 5)})
    journal(
        results / "02-generate-journal.jsonl",
        "generate",
        ("g1", "author-1", {"hypothesis": {"id": "H1"}}),
        ("g2", "author-2", None),  # started, never answered
    )
    write_json(results / "03-review-report.json", {"launch": launch("03-review.attempt2", 2.0, None, 7)})
    write_json(results / "03-review-result.json", {"status": "stopped", "cost_usd": 0.5, "usage": None})
    write_json(results / "03-review.attempt2-result.json", {"status": "completed", "cost_usd": 2.0})
    journal(results / "03-review.attempt2-journal.jsonl", "review", ("r1", "critic-1", {"verdict": "survives"}))
    return results


def test_phase_figures_are_read_from_the_reports_and_absent_stays_absent(tmp_path: Path) -> None:
    usages = {u.phase: u for u in phase_usages(pipeline_archive(tmp_path, "pl-a"))}
    assert list(usages) == PHASES
    data = usages["data"]
    assert not data.launched and data.cost_usd is None and data.total_tokens is None
    generate = usages["generate"]
    assert generate.cost_usd == 1.0 and generate.workflow_tokens == 1000
    assert generate.launcher_total == 116 and generate.total_tokens == 1116
    review = usages["review"]
    assert review.launch_stage == "03-review.attempt2"
    assert review.workflow_tokens is None and review.total_tokens == 118  # launcher only: absent is not zero
    assert review.earlier_attempts == (
        {"stage": "03-review", "status": "stopped", "cost_usd": 0.5, "workflow_tokens": None},
    )


def test_run_totals_count_earlier_attempts_and_skip_absent_figures(tmp_path: Path) -> None:
    totals = run_totals(phase_usages(pipeline_archive(tmp_path, "pl-a")))
    assert totals["cost_usd"] == pytest.approx(3.5)
    assert totals["total_tokens"] == 1116 + 118
    assert totals["workflow_tokens"] == 1000
    assert totals["launcher_tokens"] == 116 + 118


def test_a_run_without_any_launch_figure_has_absent_totals(tmp_path: Path) -> None:
    results = tmp_path / "pl-b" / "results"
    results.mkdir(parents=True)
    write_json(results / "01-data-report.json", {"sources": {}})
    assert run_totals(phase_usages(results)) == {
        "cost_usd": None,
        "total_tokens": None,
        "workflow_tokens": None,
        "launcher_tokens": None,
        "wall_s": None,
    }


def test_a_replay_counts_only_the_phase_it_re_ran(tmp_path: Path) -> None:
    usages = phase_usages(pipeline_archive(tmp_path, "pl-r", replay_of="generate"))
    assert [u.phase for u in usages if u.inherited] == ["data", "review"]
    assert run_totals(usages)["cost_usd"] == pytest.approx(1.0)


def test_story_is_built_from_the_phase_journals(tmp_path: Path) -> None:
    story = run_story(pipeline_archive(tmp_path, "pl-a"))
    exchanges = story["exchanges"]
    assert [(e["to"], e["phase"], e["stage"]) for e in exchanges] == [
        ("author-1", "generate", "02-generate"),
        ("author-2", "generate", "02-generate"),
        ("critic-1", "review", "03-review.attempt2"),
    ]
    assert all(e["timestamp"] is None and e["prompt"] is None for e in exchanges)
    assert exchanges[1]["reply"] is None
    assert exchanges[2]["verdict"] == "survives"
    narrative = story["narrative"]
    assert narrative[0] == "Phase data: no agent launch recorded."
    assert narrative[1].startswith("Phase generate: 2 agents started, 1 returned a result; workflow completed;")
    assert "CLI-reported cost $1.00" in narrative[1] and "1,116 tokens" in narrative[1]
    assert "after 1 earlier attempt(s)" in narrative[2]


def get(base: str, path: str) -> Any:
    with urllib.request.urlopen(f"{base}{path}") as response:
        return json.loads(response.read())


@pytest.fixture
def base(tmp_path: Path):
    experiments = tmp_path / "experiments"
    pipeline_archive(experiments, "pl-a")
    results = experiments / "pl-a" / "results"
    # A launcher stream whose workflow transcripts are gone, as for every archived run.
    transcript = tmp_path / "gone" / "sess-1" / "subagents" / "workflows" / "wf_1"
    write_jsonl(
        results / "02-generate-output.jsonl",
        {
            "type": "user",
            "session_id": "sess-1",
            "tool_use_result": {
                "runId": "wf_1",
                "taskId": "t1",
                "workflowName": "generate",
                "transcriptDir": str(transcript),
            },
        },
    )
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        make_handler(RunStore(tmp_path / "store"), experiments_root=experiments, smoke_root=tmp_path / "smoke"),
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def test_the_run_selector_shows_cost_and_tokens_from_the_phase_reports(base: str) -> None:
    (row,) = get(base, "/runs")
    assert row["id"] == "pl-a"
    assert row["cost_usd"] == pytest.approx(3.5)
    assert row["total_tokens"] == 1116 + 118


def test_saved_tokens_of_a_pipeline_run_are_its_phase_reports(base: str) -> None:
    (listed,) = get(base, "/tokens")
    assert listed["has_saved"] and listed["has_live"]
    data = get(base, "/tokens/pl-a?mode=saved")
    assert data["source"] == "phase_reports"
    assert [p["phase"] for p in data["by_phase"]] == PHASES
    assert data["phase_totals"]["cost_usd"] == pytest.approx(3.5)


def test_live_tokens_without_transcripts_report_absence_not_zero(base: str) -> None:
    data = get(base, "/tokens/pl-a?mode=live")
    assert data["totals"] is None
    assert [w["state"] for w in data["workflows"]] == ["missing"]
    assert data["phase_totals"]["total_tokens"] == 1116 + 118
