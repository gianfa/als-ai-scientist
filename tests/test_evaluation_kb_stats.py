"""Read-only statistics over the KB and the run archives (`evaluation/kb_stats.py`)."""

from __future__ import annotations

import json

from kb_factories import NOW, claim, decision, experiment, hypothesis, relation

from evaluation.kb_stats import all_runs, as_markdown, collect, event_stats, kb_stats, reason_class, run_stats
from knowledge_base.schemas import Event


def records(*items):
    return {r.id: r for r in items}


def event(**overrides):
    fields = {
        "id": "V-1",
        "event": "written",
        "record_id": "C-1",
        "agent_id": "literature-1",
        "run_id": "RUN-1",
        "created_at": NOW,
    }
    fields.update(overrides)
    return Event(**fields)


def test_counts_records_by_type_and_run():
    stats = kb_stats(records(claim(), claim(id="C-2", run_id="RUN-2"), relation(id="L-1", target_id="C-2")))
    assert stats["records"] == {"claim": 2, "relation": 1}
    assert stats["records_by_run"] == {"RUN-1": 2, "RUN-2": 1}
    assert stats["runs"] == ["RUN-1", "RUN-2"]


def test_a_superseded_claim_is_counted_but_leaves_the_current_set():
    stats = kb_stats(
        records(claim(), claim(id="C-2"), relation(id="L-1", type="supersedes", source_id="C-2", target_id="C-1"))
    )
    assert (stats["claims"]["total"], stats["claims"]["current"], stats["claims"]["superseded"]) == (2, 1, 1)


def test_epistemic_status_is_computed_not_read():
    """C-2 is supported only because a verified relation from a verified claim points at it."""
    stats = kb_stats(
        records(
            claim(),
            claim(id="C-2", provenance_status="unverified", verification_method="none"),
            relation(id="L-1", source_id="C-1", target_id="C-2"),
        )
    )
    assert stats["claims"]["epistemic_status"] == {"supported": 1, "unresolved": 1}


def test_verification_yield_counts_inferred_links_covered_by_a_verified_one():
    inferred = relation(id="L-1", justification_method="agent_inference", provenance_status="unverified")
    confirmed = relation(id="L-2", justification_method="quote", provenance_status="verified")
    uncovered = relation(
        id="L-3",
        source_id="C-1",
        target_id="C-3",
        justification_method="agent_inference",
        provenance_status="unverified",
    )
    stats = kb_stats(records(claim(), claim(id="C-2"), claim(id="C-3"), inferred, confirmed, uncovered))
    assert stats["verification_yield"] == {"agent_inferred_links": 2, "confirmed_as_verified": 1}


def test_hypotheses_and_experiments_are_reported_per_run():
    stats = kb_stats(records(hypothesis(), experiment(), decision(), claim()))
    assert stats["hypotheses"] == {"total": 1, "by_run": {"RUN-1": 1}, "decided": {"buy": 1}}
    assert stats["experiments"] == {"total": 1, "by_run": {"RUN-1": 1}}


def test_rejection_reasons_group_by_fault_not_by_record():
    """Two rejections of the same fault on different records must count as one recurring fault."""
    first = event(
        id="V-1",
        event="rejected",
        record_id="L-42",
        agent_id="author-2",
        detail="contradicts L-42 starts from unverified C-4: record it as questions instead",
    )
    second = event(
        id="V-2",
        event="rejected",
        record_id="L-99",
        agent_id="reviewer-1@H-3",
        detail="contradicts L-99 starts from unverified C-7: record it as questions instead",
    )
    stats = event_stats(records(first, second, event(id="V-3")))
    assert stats["by_event"]["rejected"]["by_reason"] == {"contradicts <id> starts from unverified <id>": 2}
    assert stats["by_event"]["rejected"]["by_agent"] == {"author-2": 1, "reviewer-1@H-3": 1}
    assert stats["event"] == {"rejected": 2, "written": 1} and stats["agents"] == 3


def test_reason_class_of_a_missing_detail():
    assert reason_class(None) == "(none)"


def write_run(root, run_id, phases, context=True):
    results = root / run_id / "results"
    results.mkdir(parents=True)
    (results / "state.json").write_text(
        json.dumps({"run_id": run_id, "config_hash": "h", "completed": list(phases), "phases": phases})
    )
    if context:
        (results / "context.json").write_text(json.dumps({"git_commit": "abc1234", "config": "configs/x.yaml"}))
    return root / run_id


PHASE = {
    "launch": {
        "workflow_status": "completed",
        "launcher_exit": 0,
        "session_cost_usd": 0.5,
        "wall_s": 10.0,
        "agent_count": 2,
        "workflow_usage": {"tool_uses": 7},
    },
    "written": ["C-1", "C-2"],
    "dropped_agents": ["designer@H-1"],
    "invalid_outputs": {},
    "dropped_elements": {"reviewer-1@H-1": ["a", "b"]},
    "rejected": {},
}


def test_run_stats_sums_cost_and_counts_dropped_items(tmp_path):
    run = write_run(tmp_path, "pl-1", {"literature": PHASE, "generate": PHASE})
    stats = run_stats(run)
    assert (stats["run_id"], stats["cost_usd"], stats["wall_s"]) == ("pl-1", 1.0, 20.0)
    assert stats["git_commit"] == "abc1234" and stats["completed"] == ["literature", "generate"]
    phase = stats["phases"]["literature"]
    assert (phase["written"], phase["dropped_agents"], phase["dropped_elements"]) == (2, 1, 1)
    assert (phase["agents"], phase["tool_uses"], phase["workflow_status"]) == (2, 7, "completed")


def test_a_run_without_context_json_still_reports(tmp_path):
    stats = run_stats(write_run(tmp_path, "pl-1", {"literature": PHASE}, context=False))
    assert stats["git_commit"] is None and stats["cost_usd"] == 0.5


def test_all_runs_skips_directories_without_a_state_file(tmp_path):
    write_run(tmp_path, "pl-1", {"literature": PHASE})
    (tmp_path / "not-a-run").mkdir()
    assert [r["run_id"] for r in all_runs(tmp_path)] == ["pl-1"]


def test_collect_and_markdown_render_without_a_runs_root(tmp_path):
    kb = tmp_path / "kb"
    kb.mkdir()
    (kb / "claims.jsonl").write_text(claim().model_dump_json() + "\n")
    report = collect(kb, tmp_path / "missing")
    assert "runs" not in report and report["kb"]["claims"]["total"] == 1
    text = as_markdown(report)
    assert "# Knowledge base and run statistics" in text and "Verification yield: 0 of 0" in text
