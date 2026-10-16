"""`evaluation/claim_trace.py` — the archive's record for one claim.

Every test but the last builds its own archive under `tmp_path`, so none depends on a run having been made.
The last reproduces a manual diagnosis of one archived run's claim `C-87` and skips where that archive is absent.

Three of these tests exist because the joins involved are easy to get backwards, and one of them was in fact
got backwards by hand: the claim's `origin` was read as if it named the writing phase.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from kb_factories import NOW, claim

from evaluation.claim_trace import NO_VERDICT, TraceInput, main, render, trace
from knowledge_base.schemas import Event
from knowledge_base.store import FILES

REPO_ROOT = Path(__file__).resolve().parents[1]
PROD_010 = REPO_ROOT / "experiments" / "pl-prod-010"


def write_kb(root: Path, records) -> Path:
    """A knowledge base on disk, in the store's own JSONL format, without opening it for writing."""
    root.mkdir(parents=True, exist_ok=True)
    for record in records:
        path = root / FILES[type(record)]
        with path.open("a") as handle:
            handle.write(record.model_dump_json() + "\n")
    return root


def event(
    record_id: str,
    agent_id: str,
    kind: str = "written",
    eid: str = "V-1",
    run_id: str = "RUN-1",
    detail: str | None = None,
) -> Event:
    return Event(
        id=eid, event=kind, record_id=record_id, agent_id=agent_id, detail=detail, run_id=run_id, created_at=NOW
    )


def archive(
    tmp_path: Path,
    *,
    run_id: str = "RUN-1",
    kb: Path | None = None,
    phase_reports: dict | None = None,
    tool_rows: list[dict] | None = None,
    literature: dict[str, list[dict]] | None = None,
    proposal: dict | None = None,
    tool_log_name: str = "05-review-agent-tools.jsonl",
) -> Path:
    """A minimal results directory: only what the piece under test reads."""
    results = tmp_path / "results"
    results.mkdir(parents=True, exist_ok=True)
    (results / "state.json").write_text(json.dumps({"run_id": run_id, "completed": [], "phases": {}}))
    if kb is not None:
        (results / "context.json").write_text(json.dumps({"run_id": run_id, "kb": str(kb)}))
    for name, report in (phase_reports or {}).items():
        (results / name).write_text(json.dumps(report))
    if tool_rows is not None:
        (results / tool_log_name).write_text("".join(json.dumps(r) + "\n" for r in tool_rows))
    for name, rows in (literature or {}).items():
        (results / name).write_text("".join(json.dumps(r) + "\n" for r in rows))
    if proposal is not None:
        (results / "final-proposal.json").write_text(json.dumps(proposal))
    return results


def tool_row(**overrides) -> dict:
    row = {
        "agent_id": "a8a91370801f2a72f",
        "label": "reviewer-2@H-3",
        "phase": "review",
        "model": "claude-sonnet-5",
        "ordinal": 1,
        "tool": "WebFetch",
        "at": "2026-10-10T08:20:00.000Z",
        "input_preview": '{"url": "https://example.org"}',
        "status": "ok",
        "result_bytes": 42,
        "result_preview": "some text",
    }
    row.update(overrides)
    return row


# --- trap two: origin is not a phase ------------------------------------------------------------------------


def test_the_phase_comes_from_the_phase_report_not_from_the_claims_origin(tmp_path):
    """The mistake this module exists to prevent: `origin: "literature"` on a claim a `review` agent wrote."""
    kb = write_kb(tmp_path / "kb", [claim(id="C-5", origin="literature"), event("C-5", "reviewer-2@H-3")])
    results = archive(
        tmp_path,
        kb=kb,
        phase_reports={
            "05-review-report.json": {"written": ["C-5"], "summaries": {"H-3/reviewer-2@H-3": "..."}},
            "03-literature-report.json": {"written": ["C-9"]},
        },
        tool_rows=[tool_row()],
    )
    data = trace(results, "C-5")

    assert data["who_wrote_it"]["phase_by_written_list"] == ["review"]
    assert data["who_wrote_it"]["phase_by_report_summaries"] == ["review"]
    assert data["the_claim"]["fields"]["origin"] == "literature"
    assert "not which phase wrote it" in data["the_claim"]["note"]
    # The rendered report must not let a reader take `origin` for the phase.
    assert "not which phase wrote it" in render(data)


# --- trap one: the join key ---------------------------------------------------------------------------------


def test_the_tool_log_is_joined_on_the_label_not_on_the_opaque_agent_id(tmp_path):
    """A decoy whose hashed `agent_id` equals the KB agent id must not be attributed to it."""
    kb = write_kb(tmp_path / "kb", [claim(id="C-5"), event("C-5", "reviewer-2@H-3")])
    results = archive(
        tmp_path,
        kb=kb,
        phase_reports={"05-review-report.json": {"written": ["C-5"]}},
        tool_rows=[
            tool_row(tool="WebSearch"),
            tool_row(agent_id="reviewer-2@H-3", label="reviewer-1@H-1", ordinal=2, tool="Decoy"),
        ],
    )
    called = trace(results, "C-5")["tools_called"]

    assert [row["tool"] for row in called["calls"]] == ["WebSearch"]
    assert called["state"] == "present"
    assert "opaque hash" in called["join"]


# --- trap three: an ID is not unique across bases -----------------------------------------------------------


def test_a_claim_of_another_run_in_a_shared_base_is_flagged(tmp_path):
    kb = write_kb(tmp_path / "kb", [claim(id="C-5", run_id="OTHER-RUN"), event("C-5", "x", run_id="OTHER-RUN")])
    data = trace(archive(tmp_path, run_id="RUN-1", kb=kb), "C-5")

    assert "written by run OTHER-RUN" in data["other_run"]
    assert "not unique across knowledge bases" in data["other_run"]
    assert data["other_run"] in render(data)


# --- honest absence -----------------------------------------------------------------------------------------


def test_a_missing_tool_log_an_empty_one_and_one_without_this_label_read_differently(tmp_path):
    records = [claim(id="C-5"), event("C-5", "reviewer-2@H-3")]
    states = {}
    for name, rows in (("absent", None), ("empty", []), ("no_match", [tool_row(label="someone-else")])):
        root = tmp_path / name
        kb = write_kb(root / "kb", records)
        called = trace(archive(root, kb=kb, tool_rows=rows), "C-5")["tools_called"]
        states[name] = called
        assert called["calls"] == []
        assert "no calls" not in called["reason"].lower()

    assert [states[k]["state"] for k in ("absent", "empty", "no_match")] == ["absent", "empty", "no_match"]
    # The labels that *were* seen, so a mistyped agent id is visible rather than silent.
    assert "someone-else" in states["no_match"]["reason"]


def test_a_literature_call_log_that_is_absent_is_not_reported_as_zero_checks(tmp_path):
    records = [claim(id="C-5", quote="a verbatim passage"), event("C-5", "reviewer-2@H-3")]
    bare = trace(archive(tmp_path / "a", kb=write_kb(tmp_path / "a" / "kb", records)), "C-5")
    assert bare["quote_attempts"]["logs"] == []

    with_lifecycle = trace(
        archive(
            tmp_path / "b",
            kb=write_kb(tmp_path / "b" / "kb", records),
            literature={"literature-calls.agents.jsonl": [{"event": "server_start", "store": "x"}]},
        ),
        "C-5",
    )
    [log] = with_lifecycle["quote_attempts"]["logs"]
    assert (log["state"], log["rows"], log["matched"]) == ("empty", 0, 0)  # the lifecycle row is not a check


# --- truncation ---------------------------------------------------------------------------------------------


def test_a_preview_at_the_writers_limit_is_marked_truncated(tmp_path):
    from pipeline.extract_workflow_result import INPUT_PREVIEW, RESULT_PREVIEW

    kb = write_kb(tmp_path / "kb", [claim(id="C-5"), event("C-5", "reviewer-2@H-3")])
    results = archive(
        tmp_path,
        kb=kb,
        tool_rows=[
            tool_row(ordinal=1, input_preview="x" * INPUT_PREVIEW, result_bytes=RESULT_PREVIEW + 1),
            tool_row(ordinal=2, input_preview="x" * (INPUT_PREVIEW - 1), result_bytes=RESULT_PREVIEW),
        ],
    )
    data = trace(results, "C-5")
    cut, whole = data["tools_called"]["calls"]

    assert (cut["input_truncated"], cut["result_truncated"]) == (True, True)
    assert (whole["input_truncated"], whole["result_truncated"]) == (False, False)
    # The stated limits come from the writer's own constants, so they cannot drift from the code that cuts.
    assert data["limits"]["input_preview_bytes"] == INPUT_PREVIEW
    assert data["limits"]["result_preview_bytes"] == RESULT_PREVIEW


# --- the quote-check match rules ----------------------------------------------------------------------------


def test_a_quote_check_is_found_by_the_quote_by_a_normalised_substring_and_by_the_source_ref(tmp_path):
    quote = "Peptide-coupled red blood cells induce regulatory T cells"
    records = [
        claim(id="C-5", quote=quote, source_refs=["doi:10.1073/pnas.2614908123"]),
        event("C-5", "reviewer-2@H-3"),
    ]
    rows = [
        {
            "ts": "t1",
            "paper_arg": "a",
            "paper": None,
            "quote": quote,
            "status": "unverifiable",
            "cause": "paper_not_in_store",
        },
        {
            "ts": "t2",
            "paper_arg": "b",
            "paper": None,
            "quote": "red blood cells induce regulatory T cells",
            "status": "unverifiable",
            "cause": "text_not_available",
        },
        {
            "ts": "t3",
            "paper_arg": "doi:10.1073/pnas.2614908123",
            "paper": None,
            "quote": "unrelated words",
            "status": "checked",
            "verdict": "absent",
        },
        {"ts": "t4", "paper_arg": "z", "paper": None, "quote": "nothing to do with it", "status": "checked"},
    ]
    kb = write_kb(tmp_path / "kb", records)
    attempts = trace(archive(tmp_path, kb=kb, literature={"literature-calls.jsonl": rows}), "C-5")["quote_attempts"]

    assert [(a["ts"], a["matched_by"]) for a in attempts["attempts"]] == [
        ("t1", "quote, exactly"),
        ("t2", "quote, normalised containment"),
        ("t3", "paper_arg equals a source_ref"),
    ]


# --- citation sites -----------------------------------------------------------------------------------------


def test_the_sections_and_gate_keys_that_cite_the_claim_are_listed(tmp_path):
    """The gate writes `"C-5 (url)"`, not `"C-5"`, so the first token is what must match."""
    kb = write_kb(tmp_path / "kb", [claim(id="C-5"), event("C-5", "reviewer-2@H-3")])
    proposal = {
        "proposal": {
            "intervention": {"text": "Rests on [[C-5]].", "cites": ["C-5", "C-9"]},
            "biomarker": {"text": "Not here.", "cites": ["C-9"]},
        },
        "gate": {
            "cites_an_unresolvable_source": {"intervention": ["C-5 (https://news.example/x)"]},
            "cites_judged_unfaithful": {"biomarker": ["C-9 (judged unfaithful: ...)"]},
        },
    }
    cited = trace(archive(tmp_path, kb=kb, proposal=proposal), "C-5")["where_cited"]

    assert cited["cited_in_sections"] == ["intervention"]
    assert list(cited["gate_keys_naming_it"]) == ["cites_an_unresolvable_source"]
    assert [s["sentence"] for s in cited["sentences"]] == ["Rests on [[C-5]]."]


def test_a_run_that_never_synthesized_is_still_traceable(tmp_path):
    """No `final-proposal.json`: the claim, its event and its calls still exist, so the trace must work."""
    kb = write_kb(tmp_path / "kb", [claim(id="C-5"), event("C-5", "reviewer-2@H-3")])
    data = trace(archive(tmp_path, kb=kb, tool_rows=[tool_row()]), "C-5")

    assert data["where_cited"]["state"] == "absent"
    assert "did not reach synthesis" in data["where_cited"]["reason"]
    assert data["tools_called"]["state"] == "present"


# --- dropped duplicates and the knowledge base ---------------------------------------------------------------


def test_a_dropped_duplicate_id_is_redirected_to_the_id_the_store_kept(tmp_path):
    kb = write_kb(tmp_path / "kb", [claim(id="C-91"), event("C-91", "verifier-1")])
    results = archive(
        tmp_path, kb=kb, phase_reports={"06-verify-report.json": {"written": [], "seen_again": {"C-93": "C-91"}}}
    )
    with pytest.raises(TraceInput, match=r"kept C-91 \(verify phase, seen_again\)"):
        trace(results, "C-93")


def test_the_kb_root_is_the_one_the_run_recorded(tmp_path):
    recorded = write_kb(tmp_path / "recorded", [claim(id="C-5"), event("C-5", "a")])
    beside = write_kb(tmp_path / "kb", [claim(id="C-5", content="the one beside the archive"), event("C-5", "b")])
    results = archive(tmp_path, kb=recorded)

    said = trace(results, "C-5")
    assert said["kb_root"] == str(recorded) and "context.json" in said["kb_chosen_because"]
    assert trace(results, "C-5", kb=beside)["kb_chosen_because"] == "given with --kb"

    (results / "context.json").write_text(json.dumps({"run_id": "RUN-1"}))  # no `kb` recorded
    fell_back = trace(results, "C-5")
    assert fell_back["kb_root"] == str(beside) and "beside this archive" in fell_back["kb_chosen_because"]


# --- the posture lock ---------------------------------------------------------------------------------------


def test_the_report_states_its_limits_and_gives_no_verdict(tmp_path):
    """The user's own constraint, and the one this module must not drift out of: evidence, never a label."""
    kb = write_kb(tmp_path / "kb", [claim(id="C-5", quote="a passage"), event("C-5", "reviewer-2@H-3")])
    data = trace(archive(tmp_path, kb=kb, tool_rows=[tool_row()]), "C-5")
    text = render(data)

    assert data["limits"]["no_verdict"] == NO_VERDICT
    assert NO_VERDICT in text
    # The forbidden words are forbidden as *conclusions*. The limits paragraph is where the module says it
    # draws none of them, so it is the one place they may appear, and it is removed before the check.
    findings = text.replace(NO_VERDICT, "")
    for word in ("from memory", "invented", "hallucinat", "fabricat"):
        assert word not in findings.lower(), f"the report must not conclude: {word!r}"


# --- exit codes ---------------------------------------------------------------------------------------------


def test_the_exit_codes_distinguish_a_bad_input_from_a_claim_that_is_not_there(tmp_path, capsys):
    kb = write_kb(tmp_path / "kb", [claim(id="C-5"), event("C-5", "a")])
    results = archive(tmp_path, kb=kb)

    assert main([str(results), "C-5"]) == 0
    assert main([str(results), "C-404"]) == 1
    assert main([str(tmp_path / "nowhere"), "C-5"]) == 2
    assert "claim_trace:" in capsys.readouterr().err


def test_the_json_format_carries_every_section(tmp_path, capsys):
    """`--format json` is what the real-archive test asserts on, so it must carry the whole trace."""
    kb = write_kb(tmp_path / "kb", [claim(id="C-5"), event("C-5", "a")])
    assert main([str(archive(tmp_path, kb=kb)), "C-5", "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)

    assert set(data) >= {
        "claim_id",
        "kb_root",
        "kb_chosen_because",
        "the_claim",
        "who_wrote_it",
        "tools_called",
        "quote_attempts",
        "where_cited",
        "limits",
    }
    assert data["claim_id"] == "C-5"


# --- the real archive ---------------------------------------------------------------------------------------


@pytest.mark.skipif(
    not (PROD_010 / "kb" / "claims.jsonl").is_file(), reason="no archived pl-prod-010 knowledge base in this checkout"
)
def test_pl_prod_010_C_87_reproduces_the_manual_diagnosis():
    """The case this module was written for, asserted on the facts the manual diagnosis established."""
    data = trace(PROD_010 / "results", "C-87")

    assert data["archive_run_id"] == "pl-prod-010" and "other_run" not in data
    fields = data["the_claim"]["fields"]
    assert fields["origin"] == "literature" and fields["verification_method"] == "none"
    assert data["the_claim"]["unresolved_source_refs"] == [
        "https://news.uzh.ch/en/articles/media/2026/multiple-sclerosis-autoimmune-response.html"
    ]

    wrote = data["who_wrote_it"]
    assert [e["event_id"] for e in wrote["events"]] == ["V-426"]
    assert wrote["agents"] == ["reviewer-2@H-3"]
    assert wrote["phase_by_written_list"] == ["review"], "the writing phase is review, not literature"
    assert "disagreement" not in wrote

    tools = {row["tool"] for row in data["tools_called"]["calls"]}
    assert {"WebSearch", "WebFetch", "mcp__literature__check_quote"} <= tools
    assert any(
        "doi:10.1073/pnas.2614908123" in str(row["input_preview"]) for row in data["tools_called"]["calls"]
    ), "the agent reached the real DOI"

    attempts = data["quote_attempts"]["attempts"]
    assert any(a["cause"] == "paper_not_in_store" and "news.uzh.ch" in str(a["paper_arg"]) for a in attempts)
    assert {a["log"] for a in attempts} == {"literature-calls.jsonl", "literature-calls.agents.jsonl"}
    assert all(a["during_the_run"] for a in attempts)
    # `citation_audit` was re-run on this archive to measure the OpenAlex fallback, so a third
    # log exists. Its rows are post-hoc and must not be counted among what the run was able to see.
    assert all(not a["during_the_run"] for a in data["quote_attempts"]["audit_rechecks"])

    cited = data["where_cited"]
    assert {"therapeutic_hypothesis", "intervention", "translation"} <= set(cited["cited_in_sections"])
    assert "cites_an_unresolvable_source" in cited["gate_keys_naming_it"]
