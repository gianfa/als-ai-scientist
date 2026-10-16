"""Reading a run's archive of agent tool calls: counts, who called what, and failing when nobody did."""

from __future__ import annotations

import json

import pytest

from evaluation.tool_usage import callers_of, diagnose, main, read_calls, render, summarize


def archive(results, stage, rows):
    (results / f"{stage}-agent-tools.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))


@pytest.fixture()
def results(tmp_path):
    out = tmp_path / "results"
    out.mkdir()
    archive(
        out,
        "02-generate",
        [
            {"agent_id": "a1", "label": "author-1", "tool": "mcp__kb__kb_search", "ordinal": 1},
            {"agent_id": "a1", "label": "author-1", "tool": "mcp__data__data_list", "ordinal": 2},
            {"agent_id": "a2", "label": "author-2", "tool": "StructuredOutput", "ordinal": 1},
        ],
    )
    archive(out, "01-data", [])  # a phase whose agents called nothing is not an error
    return out


def test_calls_are_read_in_stage_order_with_their_stage(results):
    calls = read_calls(results)
    assert [c["stage"] for c in calls] == ["02-generate"] * 3
    assert [c["tool"] for c in calls][:2] == ["mcp__kb__kb_search", "mcp__data__data_list"]


def test_a_malformed_line_is_skipped_rather_than_failing_the_report(results):
    with (results / "02-generate-agent-tools.jsonl").open("a") as handle:
        handle.write("{not json\n\n")
    assert len(read_calls(results)) == 3


def test_the_summary_counts_per_tool_per_stage_and_per_agent(results):
    summary = summarize(read_calls(results))
    assert summary["calls"] == 3
    assert summary["tools"]["mcp__kb__kb_search"] == 1
    assert summary["stages"]["02-generate"]["mcp__data__data_list"] == 1
    assert summary["agents"]["author-1"] == {"mcp__kb__kb_search": 1, "mcp__data__data_list": 1}


def test_callers_of_names_the_agents_and_is_empty_when_nobody_called(results):
    calls = read_calls(results)
    assert callers_of(calls, "mcp__data__") == {"02-generate": ["author-1"]}
    assert callers_of(calls, "mcp__nothing__") == {}


def test_the_cli_fails_when_the_expected_tool_was_never_called(results, capsys):
    assert main([str(results), "--expect", "mcp__data__"]) == 0
    assert main([str(results), "--expect", "mcp__absent__"]) == 1
    assert "no agent called a tool starting with 'mcp__absent__'" in capsys.readouterr().err


def test_a_missing_directory_is_a_usage_error(tmp_path):
    assert main([str(tmp_path / "absent"), "--expect", "x"]) == 2


def test_the_markdown_report_says_plainly_when_nobody_called_the_tool(results):
    calls = read_calls(results)
    text = render(summarize(calls), callers_of(calls, "mcp__absent__"), "mcp__absent__")
    assert "none: no agent called a tool starting with `mcp__absent__`" in text
    assert "`mcp__kb__kb_search` | 1" in text


CALLS = [
    {
        "label": "author-2",
        "tool": "ToolSearch",
        "input_preview": '{"query": "select:x"}',
        "stage": "02-generate",
        "status": "ok",
        "result_bytes": 128,
        "result_preview": '[{"type": "tool_reference"}]',
    },
    {
        "label": "author-2",
        "tool": "ToolSearch",
        "input_preview": '{"query": "select:x"}',
        "stage": "02-generate",
        "status": "ok",
        "result_bytes": 128,
        "result_preview": '[{"type": "tool_reference"}]',
    },
    {
        "label": "author-1",
        "tool": "mcp__kb__kb_search",
        "input_preview": '{"q": "als"}',
        "stage": "02-generate",
        "status": "ok",
        "result_bytes": 14,
        "result_preview": '{"records":[]}',
    },
    {
        "label": "author-1",
        "tool": "mcp__data__data_list",
        "input_preview": "{}",
        "stage": "02-generate",
        "status": "error",
        "result_bytes": 40,
        "result_preview": "manifest not found",
    },
]


def test_a_call_repeated_with_the_same_input_is_reported(tmp_path):
    """An agent looping on ToolSearch with the same input spends its turns without progress."""
    found = diagnose(CALLS)
    assert found["repeated_calls"] == [{"agent": "author-2", "tool": "ToolSearch", "times": 2}]
    assert found["failed_calls"][0]["tool"] == "mcp__data__data_list"


def test_a_structurally_empty_result_counts_as_empty_but_a_full_one_does_not():
    empty = diagnose(CALLS)["empty_results"]
    # `{"records":[]}` is 14 bytes, so size alone cannot tell; the ToolSearch references are not empty.
    assert [e["tool"] for e in empty] == ["mcp__kb__kb_search"]


def test_rows_without_an_outcome_are_counted_not_judged():
    found = diagnose([{"label": "a", "tool": "t", "input_preview": "{}", "stage": "s"}])
    assert found["rows_without_outcome"] == 1
    assert found["failed_calls"] == [] and found["empty_results"] == []


def test_the_diagnosis_says_so_when_there_is_nothing_to_report():
    clean = [
        {
            "label": "a",
            "tool": "t",
            "input_preview": "{}",
            "stage": "s",
            "status": "ok",
            "result_bytes": 500,
            "result_preview": '{"records": [1]}',
        }
    ]
    assert "nothing to report" in render(summarize(clean), {}, "", diagnose(clean))
