"""Overview metrics, Story tool calls, the Phase graph and the Agents view on phase-pipeline runs, read from the
phase journals, tool-call audit trails, transcripts, reports and workflow arguments. Synthetic archives only: the
real ones under experiments/ are git-ignored."""

from __future__ import annotations

import hashlib
import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from observability.wf_observer.pipeline_run import pipeline_metrics, run_phases, run_roles
from observability.wf_observer.server import make_handler
from observability.wf_observer.store import RunStore
from observability.wf_observer.story import run_story

PLAN = ["data", "generate", "review", "design"]


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def write_jsonl(path: Path, *rows: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def journal(path: Path, phase: str, *agents: tuple[str, str, bool]) -> None:
    rows: list[dict[str, Any]] = [{"type": "launched"}]
    rows += [{"type": "started", "agentId": a, "label": label, "phase": phase} for a, label, _ in agents]
    rows += [{"type": "result", "agentId": a, "result": {"ok": a}} for a, _, done in agents if done]
    write_jsonl(path, *rows)


def call(agent: str, ordinal: int, tool: str, status: str = "ok") -> dict[str, Any]:
    return {
        "agent_id": agent,
        "label": None,
        "phase": None,
        "model": "m-1",
        "ordinal": ordinal,
        "tool": tool,
        "at": f"2026-10-11T20:00:0{ordinal}.000Z",
        "input_preview": "{}",
        "status": status,
        "result_bytes": 2,
        "result_preview": "{}",
    }


def launch(stage: str, cost: float) -> dict[str, Any]:
    return {
        "stage": stage,
        "wall_s": 60.0,
        "session_cost_usd": cost,
        "workflow_status": "completed",
        "workflow_usage": {"total_tokens": 100},
    }


def archive(root: Path, run_id: str = "pl-v") -> Path:
    """data (no launch); generate (2 agents, one silent; tool trail; one transcript); review (attempt 2 after a
    stopped attempt 1, no tool trail); design planned and never reached. The run has ended (no run.pid)."""
    results = root / run_id / "results"
    write_json(results / "context.json", {"run_id": run_id, "phases": PLAN})
    write_json(results / "state.json", {"run_id": run_id, "completed": ["data", "generate", "review"], "phases": {}})
    write_json(results / "01-data-report.json", {"sources": {}})

    write_json(results / "02-generate-report.json", {"launch": launch("02-generate", 1.0)})
    write_json(results / "02-generate-result.json", {"status": "completed", "summary": "Generate phase: authors."})
    journal(results / "02-generate-journal.jsonl", "generate", ("g1", "author-1", True), ("g2", "author-2", False))
    # Out of order on disk: the view orders by `ordinal`.
    write_jsonl(
        results / "02-generate-agent-tools.jsonl",
        call("g1", 2, "mcp__kb__kb_search", "error"),
        call("g1", 1, "ToolSearch"),
    )
    write_jsonl(
        results / "transcripts" / "02-generate" / "agent-g1.jsonl",
        {"type": "user", "timestamp": "2026-10-11T19:59:59.000Z", "message": {"role": "user", "content": "PROMPT g1"}},
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "x"}]}},
    )
    write_json(
        results / "workflows" / "02-generate.args.json",
        {
            "phase": "generate",
            "run_id": run_id,
            "objective": "o",
            "schema": {"type": "object"},
            "model": "m-1",
            "web": {"searches": 0},
            "perspectives": ["a", "b"],
            "items": [{"id": 1}],
        },
    )

    write_json(results / "03-review-report.json", {"launch": launch("03-review.attempt2", 2.0)})
    write_json(results / "03-review-result.json", {"status": "stopped", "cost_usd": 0.5})
    write_json(results / "03-review.attempt2-result.json", {"status": "completed", "cost_usd": 2.0})
    journal(results / "03-review-journal.jsonl", "review", ("r0", "critic-1", False))
    journal(results / "03-review.attempt2-journal.jsonl", "review", ("r1", "critic-1", True))
    return results


def test_metrics_count_agents_and_tool_calls_from_the_archive(tmp_path: Path) -> None:
    m = pipeline_metrics("pl-v", archive(tmp_path))
    assert m["source"] == "phase_archive"
    assert (m["agents_started"], m["agents_finished"]) == (4, 2)  # both review attempts count: both were launched
    assert m["agents_by_phase"] == {"generate": {"started": 2, "finished": 1}, "review": {"started": 2, "finished": 1}}
    assert m["phase_attempts"] == 3 and m["earlier_attempts"] == 1
    assert m["tool_calls"] == 2 and m["tool_errors"] == 1 and m["tool_unanswered"] == 0
    assert m["stages_without_tool_trail"] == ["03-review", "03-review.attempt2"]
    assert m["cost_usd"] == pytest.approx(3.5)


def test_metrics_without_any_journal_or_trail_are_absent_not_zero(tmp_path: Path) -> None:
    results = tmp_path / "pl-e" / "results"
    write_json(results / "01-data-report.json", {"sources": {}})
    m = pipeline_metrics("pl-e", results)
    assert m["agents_started"] is None and m["tool_calls"] is None and m["tool_errors"] is None
    assert m["cost_usd"] is None


def test_story_shows_each_agents_tool_calls_and_archived_prompt(tmp_path: Path) -> None:
    story = run_story(archive(tmp_path))
    assert story["kind"] == "phase_pipeline"
    by_id = {e["agent_id"]: e for e in story["exchanges"]}
    g1 = by_id["g1"]
    assert g1["prompt"] == "PROMPT g1" and g1["timestamp"] == "2026-10-11T19:59:59.000Z"
    assert g1["from"] == "generate workflow"
    assert [(t["ordinal"], t["tool"], t["status"]) for t in g1["tools"]] == [
        (1, "ToolSearch", "ok"),
        (2, "mcp__kb__kb_search", "error"),
    ]
    assert by_id["g2"]["tools"] == [] and by_id["g2"]["prompt"] is None  # a trail exists: no call, not unknown
    assert by_id["r1"]["tools"] is None  # no trail for this attempt: unknown, not an empty list


def test_phase_graph_lists_phases_in_order_with_attempts_and_the_unreached_phase(tmp_path: Path) -> None:
    graph = run_phases(archive(tmp_path), {"design": ("failed", "the run ended before this phase completed")})
    phases = {p["phase"]: p for p in graph["phases"]}
    assert [p["phase"] for p in graph["phases"]] == PLAN
    assert graph["edges"] == [{"from": a, "to": b} for a, b in zip(PLAN, PLAN[1:])]
    assert phases["data"]["launched"] is False and phases["data"]["cost_usd"] is None
    gen = phases["generate"]
    assert (gen["status"], gen["agents_started"], gen["agents_finished"], gen["tool_calls"]) == ("completed", 2, 1, 2)
    assert gen["agent_labels"] == ["author-1", "author-2"]
    review = phases["review"]
    assert [(a["stage"], a["status"], a["agents_started"], a["tool_calls"]) for a in review["attempts"]] == [
        ("03-review", "stopped", 1, None),
        ("03-review.attempt2", "completed", 1, None),
    ]
    assert review["cost_usd"] == pytest.approx(2.5)
    design = phases["design"]
    assert (design["status"], design["stage"], design["cost_usd"]) == ("failed", None, None)


def test_roles_read_settings_agents_and_compare_the_role_hash(tmp_path: Path) -> None:
    results = archive(tmp_path / "experiments")
    repo = tmp_path / "repo"
    (repo / "agents").mkdir(parents=True)
    (repo / "agents" / "generate.md").write_text("You are an author.\n")
    roles = {r["phase"]: r for r in run_roles(results, repo)}
    assert roles["data"]["launched"] is False
    gen = roles["generate"]
    assert gen["summary"] == "Generate phase: authors."
    assert gen["settings"] == {"model": "m-1", "web": {"searches": 0}, "perspectives": ["a", "b"]}
    assert [(a["label"], a["finished"], a["model"]) for a in gen["agents"]] == [
        ("author-1", True, "m-1"),
        ("author-2", False, None),
    ]
    assert gen["role"]["path"] == "agents/generate.md" and gen["role_match"] is None  # no hash recorded
    assert roles["review"]["role"] is None and roles["review"]["settings"] is None

    digest = hashlib.sha256(b"You are an author.\n").hexdigest()
    manifest = results / "workflows" / "02-generate.manifest.json"
    write_json(
        manifest, [{"workflow": "02-generate.js", "role_files": [{"path": "agents/generate.md", "sha256": digest}]}]
    )
    assert {r["phase"]: r for r in run_roles(results, repo)}["generate"]["role_match"] is True
    write_json(manifest, [{"role_files": [{"path": "agents/generate.md", "sha256": "0" * 64}]}])
    assert {r["phase"]: r for r in run_roles(results, repo)}["generate"]["role_match"] is False


@pytest.fixture
def base(tmp_path: Path) -> Iterator[str]:
    experiments = tmp_path / "experiments"
    archive(experiments)
    old = experiments / "e2e-001" / "results"
    write_json(old / "context.json", {"run_id": "e2e-001"})
    (old / "run_exit_code").write_text("0")
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


def get(base: str, path: str) -> Any:
    with urllib.request.urlopen(f"{base}{path}") as response:
        return json.loads(response.read())


def test_endpoints_serve_metrics_phases_and_roles(base: str) -> None:
    metrics = get(base, "/runs/pl-v/metrics")
    assert metrics["source"] == "phase_archive" and metrics["agents_started"] == 4
    phases = get(base, "/runs/pl-v/phases")
    assert [p["status"] for p in phases["phases"]] == ["completed", "completed", "completed", "failed"]
    roles = get(base, "/runs/pl-v/roles")
    assert [r["phase"] for r in roles] == ["data", "generate", "review"]


def test_phases_of_a_run_without_phase_reports_is_404(base: str) -> None:
    with pytest.raises(urllib.error.HTTPError) as err:
        get(base, "/runs/e2e-001/phases")
    assert err.value.code == 404


def test_the_ui_offers_the_new_views_and_loads_no_remote_script(base: str) -> None:
    with urllib.request.urlopen(f"{base}/") as response:
        html = response.read().decode()
    assert "cdn." not in html and "mermaid" not in html
    with urllib.request.urlopen(f"{base}/static/js/state.js") as response:
        state = response.read().decode()
    assert "'flow'" in state and "'agents'" in state
    with urllib.request.urlopen(f"{base}/static/js/run/graph.js") as response:
        assert "href: '#agents'" not in response.read().decode()
