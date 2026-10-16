"""Scoring a run from its archive (`evaluation/run_score.py`): deterministic, over files, no agent and no network."""

from __future__ import annotations

import json
from pathlib import Path

from evaluation import run_score


def write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))


def jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def archive(tmp_path: Path) -> tuple[Path, Path]:
    """A minimal but realistic run: literature writes 3 claims, the authors link one of them and one claim that
    was in neither their slice nor the prior graph, one objection is converted and one element is dropped."""
    results, kb = tmp_path / "results", tmp_path / "kb"
    write(results / "state.json", {"run_id": "RUN-1", "completed": ["data", "compute", "literature", "generate"]})
    write(results / "03-literature-result.json", {"cost_usd": 1.5, "agent_count": 2, "status": "completed"})
    write(results / "04-generate-result.json", {"cost_usd": 0.5, "agent_count": 2, "status": "completed"})
    write(results / "03-literature-report.json", {"written": ["C-10", "C-11", "C-12", "L-1"]})
    write(
        results / "04-generate-report.json",
        {
            "written": ["C-20", "L-2", "L-3", "H-1"],
            "dropped_elements": {"author-1": ["claim e9: Value error"]},
            "converted_elements": {"author-1": ["contradicts C-99 -> C-10: source claim is not verified, kept as I-1"]},
            "invalid_outputs": {},
            "dropped_agents": [],
        },
    )
    write(
        results / "workflows" / "04-generate.args.json",
        {
            "kb_context": [
                {"id": "C-10", "provenance_status": "verified", "origin": "literature"},
                {"id": "C-1", "provenance_status": "verified", "origin": "literature"},
                {"id": "C-2", "provenance_status": "unverified", "origin": "computation"},
            ],
            "prior_graph": {"edges": [{"claim_id": "C-11"}]},
            "schema": {"properties": {"hypothesis": {"properties": {"author_confidence": {"type": "integer"}}}}},
        },
    )
    jsonl(
        results / "04-generate-agent-tools.jsonl",
        [
            {
                "label": "author-1",
                "tool": "mcp__kb__kb_search",
                "status": "ok",
                "input_preview": '{"query": "il-10", "types": ["Claim"]}',
                "result_preview": '{"records": [{"id": "C-1"}]}',
                "result_bytes": 28,
            },
            {
                "label": "author-1",
                "tool": "mcp__kb__kb_search",
                "status": "ok",
                "input_preview": '{"query": "nothing here"}',
                "result_preview": '{"records": []}',
                "result_bytes": 14,
            },
            {
                "label": "author-2",
                "tool": "StructuredOutput",
                "status": "ok",
                "input_preview": '{"hypothesis": {"author_confidence": 2}}',
                "result_preview": "{}",
                "result_bytes": 2,
            },
        ],
    )
    jsonl(
        kb / "relations.jsonl",
        [
            {"id": "L-2", "source_id": "C-20", "target_id": "C-10", "type": "supports"},
            {"id": "L-3", "source_id": "C-20", "target_id": "C-50", "type": "supports"},
        ],
    )
    return results, kb


def test_the_score_reads_every_measurement_from_the_archive(tmp_path):
    results, kb = archive(tmp_path)
    data = run_score.score(results, kb)

    assert data["run_id"] == "RUN-1"
    # An archive without `execute` and `reassess` is missing them.
    assert data["phases"]["missing"] == [
        "review",
        "verify",
        "design",
        "arbitrate",
        "execute",
        "reassess",
        "intervene",
        "synthesize",
    ]
    assert data["cost"]["total_usd"] == 2.0

    # Of the 3 claims the literature phase wrote, the authors linked exactly one (C-10, through L-2).
    assert data["evidence_reach"]["fresh_claims"] == 3
    assert data["evidence_reach"]["touched_by_authors"] == 1
    assert data["evidence_reach"]["claims"] == ["C-10"]

    # The slice: 3 claims, 2 verified, 1 computed, 1 of them written by this run's literature phase.
    assert data["slice"] == {"claims": 3, "verified": 2, "computed": 1, "fresh": 1}

    # C-50 is in neither the slice nor the prior graph, so it can only have come from a tool answer.
    assert data["citations"]["claims_cited"] == 2
    assert data["citations"]["from_neither_slice_nor_prior_graph"] == 1
    assert data["citations"]["outside"] == ["C-50"]

    assert data["search"] == {
        "calls": 2,
        "with_types": 1,
        "empty": 1,
        "by_stage": {"04-generate": {"calls": 2, "with_types": 1, "empty": 1}},
    }

    # Elements, not agents: one dropped, one objection kept instead of dropped.
    assert data["losses"]["dropped_elements"] == {"generate": 1}
    assert data["losses"]["converted_elements"] == {"generate": 1}
    assert "kept as I-1" in data["losses"]["converted_messages"]["generate"][0]

    # An author declaring its own confidence is not a leak: neither the output schema nor its StructuredOutput
    # input counts, and nothing else carries an author field here.
    assert data["blinding"] == {"leaks": 0, "where": []}

    assert data["result"]["outcome"] is None and "did not reach synthesis" in data["result"]["reason"]
    assert data["verify"]["verdicts"] is None


def test_a_leak_in_the_pushed_slice_is_reported(tmp_path):
    results, kb = archive(tmp_path)
    args = json.loads((results / "workflows" / "04-generate.args.json").read_text())
    args["items"] = [
        {"hypothesis_id": "H-1", "context": [{"id": "H-1", "agent_id": "author-1", "author_confidence": 2}]}
    ]
    write(results / "workflows" / "04-generate.args.json", args)
    data = run_score.score(results, kb)
    assert data["blinding"]["leaks"] == 2
    assert {w["field"] for w in data["blinding"]["where"]} == {"agent_id", "author_confidence"}
    assert "**Author fields reached an agent**" in run_score.render(data)


def test_a_missing_input_is_reported_as_unmeasured_not_as_zero(tmp_path):
    """A partial archive must not read as a run that measured zero: the reason is said, and the value is null."""
    results = tmp_path / "results"
    write(results / "state.json", {"run_id": "RUN-2", "completed": ["data"]})
    data = run_score.score(results, tmp_path / "absent-kb")
    assert data["evidence_reach"]["fresh_claims"] is None
    assert "no literature report" in data["evidence_reach"]["reason"]
    assert data["slice"] is None and "no generate args" in data["reason"]
    assert data["result"]["outcome"] is None
    rendered = run_score.render(data)
    assert "| — |" in rendered and "Not measured:" in rendered


def test_the_gate_flag_and_the_verifier_verdicts_are_surfaced(tmp_path):
    results, kb = archive(tmp_path)
    write(
        results / "06-verify-report.json",
        {
            "written": [],
            "verdicts": {
                "L-1": {"verdict": "confirmed"},
                "L-2": {"verdict": "rejected"},
                "L-3": {"verdict": "insufficient"},
            },
        },
    )
    write(
        results / "final-proposal.json",
        {
            "proposal": {
                "outcome": "proposal",
                "failure_modes": [1, 2, 3, 4, 5],
                "mechanism": {"cites": ["C-10", "C-11"]},
                "validation": {"cites": ["C-12"]},
            },
            "gate": {
                "passed": True,
                "cites_contradicted_or_contested": {"mechanism": ["C-10 (contradicted)"]},
                "computed_claims_cited": [],
            },
        },
    )
    jsonl(
        kb / "experiments.jsonl",
        [
            {"id": "EXP-1", "run_id": "RUN-1", "kind": "in_silico"},
            {"id": "EXP-2", "run_id": "RUN-1", "kind": "wet_lab"},
            {"id": "EXP-3", "run_id": "OTHER", "kind": "wet_lab"},
        ],
    )
    jsonl(kb / "decisions.jsonl", [{"id": "D-1", "run_id": "RUN-1", "decision": "buy", "experiment_ids": ["EXP-1"]}])
    data = run_score.score(results, kb)
    assert data["result"]["experiments_proposed"] == 2  # the other run's experiment is not ours
    assert data["result"]["experiments_by_kind"] == {"in_silico": 1, "wet_lab": 1}
    assert data["result"]["experiments_bought"] == 1 and data["result"]["cites_total"] == 3
    assert data["verify"]["counts"] == {"confirmed": 1, "rejected": 1, "insufficient": 1}
    rendered = run_score.render(data)
    assert "The gate flagged citations of contradicted or contested claims" in rendered
    assert "C-10 (contradicted)" in rendered


def test_a_confirmed_verdict_on_an_unfaithful_claim_is_counted_apart(tmp_path):
    """It leaves no trace in the knowledge base — `Builder.confirmed` needs the verdict *and* `faithful` — so a
    run can confirm contradictions and still end with no contradicted claim. That is what happened on the
    production configuration, and the count is what makes it visible."""
    results, kb = archive(tmp_path)
    jsonl(
        kb / "relations.jsonl",
        [
            {"id": "L-2", "source_id": "C-20", "target_id": "C-10", "type": "supports"},
            {"id": "L-3", "source_id": "C-20", "target_id": "C-50", "type": "supports"},
            {"id": "L-4", "source_id": "C-21", "target_id": "C-10", "type": "contradicts"},
            {"id": "L-5", "source_id": "C-22", "target_id": "C-10", "type": "contradicts"},
        ],
    )
    write(
        results / "06-verify-report.json",
        {
            "written": [],
            "verdicts": {
                "L-2": {"verdict": "confirmed", "faithful": True},
                "L-4": {"verdict": "confirmed", "faithful": False},
                "L-5": {"verdict": "insufficient", "faithful": False},
                "L-9": {"verdict": "confirmed", "faithful": False},
            },
        },
    )

    data = run_score.score(results, kb)

    assert data["verify"]["by_relation_type"] == {
        "supports": {"verdicts": 1, "confirmed_and_faithful": 1, "confirmed_not_faithful": 0},
        "contradicts": {"verdicts": 2, "confirmed_and_faithful": 0, "confirmed_not_faithful": 1},
        "unknown": {"verdicts": 1, "confirmed_and_faithful": 0, "confirmed_not_faithful": 1},
    }
    assert data["verify"]["confirmed_discarded_as_unfaithful"] == 2
    assert "confirmed but discarded as unfaithful" in run_score.render(data)


def test_without_the_relations_the_split_by_type_is_null_with_its_reason():
    data = run_score.verify_verdicts({"verify": {"verdicts": {"L-1": {"verdict": "confirmed", "faithful": True}}}})

    assert data["counts"] == {"confirmed": 1}
    assert data["by_relation_type"] is None and data["reason"] == "relation types not available"


def test_the_cli_prints_json_and_markdown(tmp_path, capsys):
    results, kb = archive(tmp_path)
    assert run_score.main([str(results), "--kb", str(kb)]) == 0
    assert json.loads(capsys.readouterr().out)["run_id"] == "RUN-1"
    assert run_score.main([str(results), "--kb", str(kb), "--format", "markdown"]) == 0
    assert "# Run score — RUN-1" in capsys.readouterr().out
    assert run_score.main([str(tmp_path / "nope")]) == 2


def test_saving_writes_both_formats_and_never_overwrites(tmp_path, capsys):
    results, kb = archive(tmp_path)
    data = run_score.score(results, kb)
    first = run_score.save(data, results)
    assert [p.name for p in first] == ["run-score.json", "run-score.md"]
    assert json.loads(first[0].read_text())["run_id"] == "RUN-1"
    assert first[1].read_text().startswith("# Run score — RUN-1")
    # A second scoring of the same archive lands beside the first, so two readings can be compared.
    second = run_score.save(data, results)
    assert [p.name for p in second] == ["run-score.2.json", "run-score.2.md"]
    assert first[0].exists() and json.loads(first[0].read_text())["run_id"] == "RUN-1"
    # The score is not written under results/analysis/, which belongs to the compute phase's artifacts.
    assert not list((results / "analysis").glob("run-score*")) if (results / "analysis").is_dir() else True
    assert run_score.main([str(results), "--kb", str(kb), "--save"]) == 0
    assert "wrote" in capsys.readouterr().err


def test_filesystem_reach_separates_inside_transcript_refused_and_unparsed(tmp_path):
    """The reach check: 0 successful calls outside the phase directory is the expected value."""
    results = tmp_path / "results"
    stage_dir = results / "workflows" / "06-verify"
    calls = [
        # Inside: the workflow script the Workflow tool must read, by absolute path and relatively.
        {
            "stage": "06-verify",
            "label": "v-1",
            "tool": "Read",
            "status": "ok",
            "input_preview": json.dumps({"file_path": str(stage_dir / "06-verify.js")}),
        },
        {
            "stage": "06-verify",
            "label": "v-1",
            "tool": "Read",
            "status": "ok",
            "input_preview": json.dumps({"file_path": "./06-verify.js"}),
        },
        # Outside and refused: the restriction denies it.
        {
            "stage": "06-verify",
            "label": "v-2",
            "tool": "Read",
            "status": "error",
            "input_preview": json.dumps({"file_path": "/app/als-ai-scientist/data"}),
        },
        # Outside and succeeded: the one number that must stay at 0.
        {
            "stage": "05-review",
            "label": "r-1",
            "tool": "Read",
            "status": "ok",
            "input_preview": json.dumps({"file_path": "/app/als-ai-scientist/dist/p.md"}),
        },
        # Truncated past the path: counted, not guessed at.
        {
            "stage": "05-review",
            "label": "r-2",
            "tool": "Grep",
            "status": "ok",
            "input_preview": '{"pattern": "Treg", "output_mode": "files_with_matches"}',
        },
        # The CLI's own transcript tree: readable whatever the cwd, so counted apart rather than as a reach.
        {
            "stage": "05-review",
            "label": "r-1",
            "tool": "Read",
            "status": "ok",
            "input_preview": json.dumps({"file_path": "/home/u/.claude/projects/p/s/tool-results/webfetch-1.txt"}),
        },
        # Not a filesystem tool.
        {
            "stage": "05-review",
            "label": "r-2",
            "tool": "mcp__kb__kb_search",
            "status": "ok",
            "input_preview": json.dumps({"file_path": "/etc/passwd"}),
        },
    ]
    data = run_score.filesystem_reach(results, calls)
    assert data == {
        "calls": 6,
        "inside": 2,
        "unparsed": 1,
        "transcript": 1,
        "outside_succeeded": 1,
        "outside_refused": 1,
        "where": [
            {
                "where": "06-verify:v-2:Read",
                "succeeded": False,
                "path": "/app/als-ai-scientist/data",
            },
            {
                "where": "05-review:r-1:Read",
                "succeeded": True,
                "path": "/app/als-ai-scientist/dist/p.md",
            },
        ],
    }


def test_filesystem_reach_is_clean_when_no_agent_touched_a_file(tmp_path):
    data = run_score.filesystem_reach(tmp_path / "results", [{"tool": "mcp__kb__kb_get", "status": "ok"}])
    assert data == {
        "calls": 0,
        "inside": 0,
        "unparsed": 0,
        "transcript": 0,
        "outside_succeeded": 0,
        "outside_refused": 0,
        "where": [],
    }


# The experiment readout. Its field names must follow `Orchestrator._execute_phase` and `._reassess_phase`: a
# readout keyed on names the orchestrator does not write reports nothing and looks like a run that did nothing.

DIGEST = "56cabd85" + "0" * 56
ICS_DIGEST = "11aa22bb" + "0" * 56
EXECUTION = {
    "experiment_id": "EXP-91",
    "analysis_id": "hla_panel_compact_v1",
    "hypothesis_id": "H-7",
    "tested_claims": ["C-784"],
    "status": "executed",
    "result_digest": DIGEST,
    "artifact": "experiment-analysis/EXP-91/summary.json",
    "interpretation_check": {
        "faithful": True,
        "errors": [],
        "outcome": "panel_condition_met",
        "verified_result_digest": DIGEST,
    },
    "written": ["C-801", "C-802"],
    "seen_again": {},
}
EXECUTED = {
    "selected": [
        {
            "experiment_id": "EXP-91",
            "analysis_id": "hla_panel_compact_v1",
            "hypothesis_id": "H-7",
            "tested_claims": ["C-784"],
            "dataset_refs": ["dataset:michaelis-table1-epitopes@89af13c3630a"],
        }
    ],
    "skipped": [{"experiment_id": "EXP-90", "reason": "no registered execution handler"}],
    "status": "executed",
    "executed_count": 1,
    "executions": [EXECUTION],
}


def test_experiment_readout_reads_an_executed_experiment():
    row = run_score.experiment_run(
        {
            "execute": EXECUTED,
            "reassess": {
                "status": "judged",
                "judged_count": 1,
                "judgements": [
                    {
                        "experiment_id": "EXP-91",
                        "hypothesis_id": "H-7",
                        "status": "judged",
                        "judgement": {"decision": "keep", "claim_ids": ["C-801"]},
                    }
                ],
            },
        }
    )
    assert row["executed"] is True
    assert (row["eligible_bought"], row["executed_count"]) == (1, 1)
    assert row["analyses_executed"] == ["hla_panel_compact_v1"]
    assert row["skipped_bought_experiments"][0]["experiment_id"] == "EXP-90"
    assert (row["reassess_status"], row["judged_count"]) == ("judged", 1)
    (execution,) = row["executions"]
    assert (execution["experiment_id"], execution["analysis_id"]) == ("EXP-91", "hla_panel_compact_v1")
    assert execution["outcome"] == "panel_condition_met" and execution["interpretation_faithful"] is True
    assert execution["result_digest"].startswith("56cabd85")
    assert execution["computed_claims_written"] == ["C-801", "C-802"]
    assert (execution["reassess_status"], execution["reassessment"]) == ("judged", "keep")


def test_the_reassessment_decision_reaches_the_row():
    """Before the fix this read `judgement`/`verdict`; `ReassessOutput` carries neither, so no scorecard ever
    recorded a verdict — every archive scored `reassessment: null` whatever the critic had decided."""
    row = run_score.experiment_run(
        {
            "execute": EXECUTED,
            "reassess": {
                "status": "judged",
                "judged_count": 1,
                "judgements": [
                    {
                        "experiment_id": "EXP-91",
                        "status": "judged",
                        "judgement": {"decision": "inconclusive", "claim_ids": ["C-801"]},
                    }
                ],
            },
        }
    )
    assert row["executions"][0]["reassessment"] == "inconclusive"


def test_each_executed_experiment_keeps_its_own_row_and_its_own_judgement():
    """A run that executes two handlers must not read as a run that executed one."""
    second = EXECUTION | {
        "experiment_id": "EXP-92",
        "analysis_id": "ics_cytokine_skew_v1",
        "hypothesis_id": "H-8",
        "result_digest": ICS_DIGEST,
        "artifact": "experiment-analysis/EXP-92/summary.json",
        "interpretation_check": {
            "faithful": True,
            "errors": [],
            "outcome": "skew_replicated",
            "verified_result_digest": ICS_DIGEST,
        },
        "written": [],
        "seen_again": {"C-900": "C-801"},
    }
    execute = EXECUTED | {"executions": [EXECUTION, second], "executed_count": 2, "selected": EXECUTED["selected"] * 2}
    row = run_score.experiment_run(
        {
            "execute": execute,
            "reassess": {
                "status": "judged",
                "judged_count": 1,
                "judgements": [
                    {
                        "experiment_id": "EXP-91",
                        "status": "judged",
                        "judgement": {"decision": "keep", "claim_ids": ["C-801"]},
                    },
                    {
                        "experiment_id": "EXP-92",
                        "status": "unverified",
                        "error": "result_digest is not the digest of this execution",
                    },
                ],
            },
        }
    )
    assert row["executed_count"] == 2
    assert row["analyses_executed"] == ["hla_panel_compact_v1", "ics_cytokine_skew_v1"]
    assert [e["experiment_id"] for e in row["executions"]] == ["EXP-91", "EXP-92"]
    assert row["executions"][1]["computed_claims_seen_again"] == ["C-801"]
    assert row["executions"][1]["reassessment"] is None
    assert "not the digest" in row["executions"][1]["reassess_error"]


def test_a_reused_result_is_one_computation_not_two():
    """Two hypotheses bound to one computation are two rows, two judgements and one measurement."""
    reuse = EXECUTION | {
        "experiment_id": "EXP-93",
        "hypothesis_id": "H-9",
        "result_of": "EXP-91",
        "written": [],
        "seen_again": {},
        "result_claims": ["C-801", "C-802"],
    }
    execute = EXECUTED | {"executions": [EXECUTION, reuse], "executed_count": 1, "reused_count": 1}
    row = run_score.experiment_run({"execute": execute})
    assert (row["executed_count"], row["reused_count"], row["computations"]) == (1, 1, 1)
    assert [e["result_of"] for e in row["executions"]] == [None, "EXP-91"]
    # An archive written before the field existed scores `reused_count` as absent, never as zero.
    assert run_score.experiment_run({"execute": EXECUTED})["reused_count"] is None


def test_experiment_readout_keeps_the_reason_a_run_executed_nothing():
    """`not_executed` is a legitimate outcome and must be scored as one, with its reason, not as silence."""
    row = run_score.experiment_run(
        {
            "execute": {
                "selected": [],
                "executions": [],
                "skipped": [{"experiment_id": "EXP-90", "reason": "data feasibility is missing_inputs"}],
                "status": "not_executed",
                "reason": "no bought EXP has a registered handler and supported inputs",
            }
        }
    )
    assert row["executed"] is False
    assert row["not_executed_reason"] == "no bought EXP has a registered handler and supported inputs"
    assert row["skipped_bought_experiments"][0]["reason"] == "data feasibility is missing_inputs"
    assert (row["eligible_bought"], row["executions"]) == (0, [])
    assert row["reassess_status"] is None


def test_experiment_readout_reports_an_unfaithful_interpretation():
    unfaithful = EXECUTION | {
        "interpretation_check": {
            "faithful": False,
            "outcome": "panel_condition_met",
            "errors": ["missing limitation: Panel coverage is not population coverage."],
            "verified_result_digest": DIGEST,
        }
    }
    execute = EXECUTED | {"executions": [unfaithful]}
    row = run_score.experiment_run(
        {
            "execute": execute,
            "reassess": {
                "status": "unverified",
                "judged_count": 0,
                "judgements": [{"experiment_id": "EXP-91", "status": "unverified", "error": "no output"}],
            },
        }
    )
    assert row["executed"] is True
    (execution,) = row["executions"]
    assert execution["interpretation_faithful"] is False
    assert execution["interpretation_errors"] == ["missing limitation: Panel coverage is not population coverage."]
    assert execution["reassess_error"] == "no output"


def test_experiment_readout_says_so_when_the_phase_never_ran():
    row = run_score.experiment_run({"literature": {}})
    assert row["executed"] is None and "did not run" in row["reason"]


def test_narrowing_separates_proposed_from_refused():
    """`not_refused` is deliberately not called `written`: the phase report cannot tell a new claim from one
    that met an existing record."""
    report = {
        "verdicts": {
            "L-1": {"verdict": "confirmed", "faithful": False, "narrowed": "a narrower form"},
            "L-2": {"verdict": "confirmed", "faithful": False, "narrowed": "another"},
            "L-3": {"verdict": "insufficient", "faithful": False, "narrowed": None},
        },
        "not_narrowed": {"L-2": "the restatement's quote could not be verified against the source"},
    }
    row = run_score.verify_verdicts({"verify": report}, relations={})
    assert row["narrowing"] == {
        "proposed": 2,
        "refused": 1,
        "not_refused": 1,
        "refusal_reasons": ["the restatement's quote could not be verified against the source"],
    }


def test_quote_checks_rate_ignores_unverifiable_and_counts_unparsed():
    calls = [
        {
            "tool": "mcp__literature__check_quote",
            "phase": "03-literature",
            "result_preview": '{"status":"checked","verdict":"exact","score":1}',
        },
        {
            "tool": "mcp__literature__check_quote",
            "phase": "03-literature",
            "result_preview": '{"status":"checked","verdict":"absent"}',
        },
        {
            "tool": "mcp__literature__check_quote",
            "phase": "03-literature",
            "result_preview": '{"status":"unverifiable","verdict":null,"cause":"no stored text"}',
        },
        {"tool": "mcp__literature__check_quote", "phase": "03-literature", "result_preview": '{"stat'},
    ]
    row = run_score.quote_checks(calls)["03-literature"]
    assert row["calls"] == 4 and row["judged"] == 2
    assert row["exact_rate_of_judged"] == 0.5 and row["absent"] == 1
    assert row["verdicts"] == {"exact": 1, "absent": 1, "unverifiable": 1, "unparsed": 1}


def test_paper_tool_reports_an_unreadable_flag_rather_than_a_zero():
    """A truncated preview that does not carry `outside_main_text` must not be counted as `false`."""
    calls = [
        {"tool": "mcp__paper__paper_navigate", "phase": "03-literature", "result_preview": "{}"},
        {
            "tool": "mcp__paper__paper_passage",
            "phase": "03-literature",
            "result_preview": '{"status":"served","outside_main_text":true}',
        },
        {
            "tool": "mcp__paper__paper_passage",
            "phase": "03-literature",
            "result_preview": '{"status":"served","lines":[1,9],"text":"truncated here',
        },
    ]
    row = run_score.paper_tool_use(calls)["03-literature"]
    assert row == {"paper_navigate": 1, "paper_passage": 2, "outside_main_text_true": 1, "flag_unreadable": 1}


# --- output A measured as a shape, not as a verdict ---------------------------------------------------------


def mechanism_archive(tmp_path: Path, cites: list[str]) -> tuple[Path, Path]:
    """An archive whose mechanism section cites `cites`, over a KB holding a four-edge chain and two strays."""
    results, kb = tmp_path / "results", tmp_path / "kb"
    write(results / "final-proposal.json", {"proposal": {"mechanism": {"text": "...", "cites": cites}}})
    chain = [
        ("C-1", "N-1", "N-2", "verified"),
        ("C-2", "N-2", "N-3", "unverified"),
        ("C-3", "N-3", "N-4", "unverified"),
        ("C-4", "N-4", "N-5", "unverified"),
    ]
    stray = [("C-9", "N-8", "N-5", "unverified"), ("C-8", "N-7", "N-5", "unverified")]
    jsonl(
        kb / "claims.jsonl",
        [{"id": i, "cause_ref": c, "effect_ref": e, "provenance_status": p} for i, c, e, p in chain + stray]
        + [{"id": "C-50", "content": "not a causal claim", "provenance_status": "verified"}],
    )
    jsonl(
        results / "12-synthesize-agent-tools.jsonl",
        [
            {
                "label": "pi",
                "phase": "synthesize",
                "tool": "mcp__kb__kb_mechanism_path",
                "status": "ok",
                "input_preview": "{}",
                "result_preview": "{}",
                "result_bytes": 10,
            }
        ],
    )
    return results, kb


def test_the_chain_is_measured_by_length_and_the_strays_are_named(tmp_path):
    results, kb = mechanism_archive(tmp_path, ["C-1", "C-2", "C-3", "C-4", "C-9", "C-8", "C-50"])
    chain = run_score.mechanism_chain(results, kb, run_score.read_jsonl(results / "12-synthesize-agent-tools.jsonl"))

    assert chain["chain"] == ["C-1", "C-2", "C-3", "C-4"] and chain["chain_edges"] == 4
    assert (chain["cited"], chain["cited_causal"], chain["cited_verified"]) == (7, 6, 1)
    # The two edges that merely point at the chain's last node are cited and off it; C-50 is not causal at all.
    assert chain["off_chain"] == ["C-9", "C-8"]
    assert chain["chain_verified"] == ["C-1"]
    assert chain["joins"] and chain["gate_requirement_met"]
    assert chain["path_tool_calls"] == {"synthesize": 1}


def test_the_chain_steps_name_both_ends_in_order_and_say_when_a_name_is_missing(tmp_path):
    """The figure reads the chain from the scorecard alone, so the scorecard must carry what it draws."""
    results, kb = mechanism_archive(tmp_path, ["C-1", "C-2"])
    jsonl(
        kb / "entities.jsonl",
        [{"id": "N-1", "canonical_name": "repeat expansion"}, {"id": "N-2", "canonical_name": "Treg"}],
    )
    steps = run_score.mechanism_chain(results, kb, [])["chain_steps"]
    assert [s["claim_id"] for s in steps] == ["C-1", "C-2"]
    assert "sign" not in steps[0]  # ADR 0032
    assert (steps[0]["cause_name"], steps[0]["effect_name"], steps[0]["provenance_status"]) == (
        "repeat expansion",
        "Treg",
        "verified",
    )
    # N-3 is in no entity record: its name is absent, not invented from the ID.
    assert steps[1]["effect_ref"] == "N-3" and steps[1]["effect_name"] is None


def test_a_passing_gate_and_a_two_edge_chain_are_not_reported_as_the_same_result(tmp_path):
    """The gate says pass either way; the measurement has to tell nine edges from two."""
    results, kb = mechanism_archive(tmp_path, ["C-1", "C-2"])
    chain = run_score.mechanism_chain(results, kb, [])
    assert (chain["chain_edges"], chain["gate_requirement_met"]) == (2, True)


def test_cited_edges_that_do_not_join_are_reported_as_no_chain(tmp_path):
    results, kb = mechanism_archive(tmp_path, ["C-1", "C-9"])
    chain = run_score.mechanism_chain(results, kb, [])
    assert chain["chain_edges"] == 1 and chain["joins"] is False
    assert chain["gate_requirement_met"] is False
    assert chain["off_chain"] == ["C-9"]


def test_a_verified_edge_outside_the_chain_does_not_satisfy_the_requirement(tmp_path):
    """`check_mechanism` wants a verified step; a verified claim cited beside the chain is not one."""
    results, kb = tmp_path / "results", tmp_path / "kb"
    write(results / "final-proposal.json", {"proposal": {"mechanism": {"cites": ["C-2", "C-3", "C-7"]}}})
    jsonl(
        kb / "claims.jsonl",
        [
            {"id": "C-2", "cause_ref": "N-2", "effect_ref": "N-3", "provenance_status": "unverified"},
            {"id": "C-3", "cause_ref": "N-3", "effect_ref": "N-4", "provenance_status": "unverified"},
            {"id": "C-7", "cause_ref": "N-9", "effect_ref": "N-10", "provenance_status": "verified"},
        ],
    )
    chain = run_score.mechanism_chain(results, kb, [])
    assert chain["chain"] == ["C-2", "C-3"] and chain["cited_verified"] == 1
    assert chain["chain_verified"] == [] and chain["gate_requirement_met"] is False


def test_a_run_without_a_proposal_is_not_evaluable_rather_than_zero(tmp_path):
    results, kb = tmp_path / "results", tmp_path / "kb"
    results.mkdir(parents=True)
    kb.mkdir(parents=True)
    assert run_score.mechanism_chain(results, kb, [])["cited_causal"] is None
    write(results / "final-proposal.json", {"proposal": {"outcome": "insufficient_evidence"}})
    absent = run_score.mechanism_chain(results, kb, [])
    assert absent["cited_causal"] is None and "no mechanism section" in absent["reason"]


def test_a_run_with_executions_renders_one_line_each(tmp_path):
    """`render` read `selected_experiment_id`, a field the experiment block lost in an earlier change, so the first archive
    scored after it raised `KeyError` before anything was written."""
    results, kb = archive(tmp_path)
    data = run_score.score(results, kb)
    reuse = EXECUTION | {"experiment_id": "EXP-93", "hypothesis_id": "H-9", "result_of": "EXP-91"}
    data["experiment"] = run_score.experiment_run(
        {"execute": EXECUTED | {"executions": [EXECUTION, reuse], "executed_count": 1, "reused_count": 1}}
    )
    rendered = run_score.render(data)
    assert "| bought experiments executed (computations, reused, analyses) | 1, 1, hla_panel_compact_v1 |" in rendered
    assert "Experiment `EXP-91` ran `hla_panel_compact_v1` on hypothesis `H-7`" in rendered
    assert "`EXP-93` ran `hla_panel_compact_v1` (the result of `EXP-91`)" in rendered
