"""`evaluation/quote_outcomes.py` — quote-check outcomes per run and per stage.

Each test builds its own archive under `tmp_path` as real JSONL, the idiom of
`tests/test_evaluation_tool_usage.py`. The last reproduces the measurement taken by hand over the real
archives and skips where they are absent.

Three of these exist because the throwaway scripts that first answered this question got it wrong in exactly
these ways: they counted three different logs as one leg, they reported per run rather than per stage, and they
would have read a cause lost to truncation as a success.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from evaluation.quote_outcomes import (
    CAUSES,
    FATES,
    FETCH_OUTCOMES,
    LATER,
    NEVER_CITED,
    OUTCOMES,
    UNPARSED,
    VERDICTS,
    OutcomesInput,
    aggregate,
    attribute,
    collect,
    dead_ends,
    main,
    order_stages,
    outcome_of,
    outcome_of_preview,
    phase_of,
    render,
    stage_windows,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PROD = REPO_ROOT / "experiments"
RUNS = (
    "pl-prod-001",
    "pl-prod-002",
    "pl-prod-003",
    "pl-prod-004",
    "pl-prod-005",
    "pl-prod-006",
    "pl-prod-007",
    "pl-prod-008",
    "pl-prod-009",
    "pl-prod-010",
    "pl-replay-007-reassess",
    "pl-smoke-010",
    "pl-smoke-011",
    "pl-smoke-012",
)


def jsonl(path: Path, rows) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def archive(
    tmp_path: Path,
    *,
    run_id: str = "RUN-1",
    tool: dict[str, list[dict]] | None = None,
    host: list[dict] | None = None,
    agents: list[dict] | None = None,
    audit: list[dict] | None = None,
    fetched: dict[str, dict] | None = None,
    reports: dict[str, dict] | None = None,
) -> Path:
    results = tmp_path / "results"
    results.mkdir(parents=True, exist_ok=True)
    (results / "state.json").write_text(json.dumps({"run_id": run_id, "completed": [], "phases": {}}))
    for stage, rows in (tool or {}).items():
        jsonl(results / f"{stage}-agent-tools.jsonl", rows)
    for name, rows in (
        ("literature-calls.jsonl", host),
        ("literature-calls.agents.jsonl", agents),
        ("literature-calls.audit.jsonl", audit),
    ):
        if rows is not None:
            jsonl(results / name, rows)
    for phase, got in (fetched or {}).items():
        (results / f"03-{phase}-report.json").write_text(json.dumps({"written": [], "fetched": got}))
    for name, report in (reports or {}).items():
        (results / name).write_text(json.dumps(report))
    return results


def call(
    stage_ordinal: int = 1,
    *,
    preview: str = '{"status":"checked","verdict":"exact"}',
    paper: str = "doi:10.1038/x",
    label: str = "reviewer-1@H-1",
    at: str = "2026-10-10T08:20:00.000Z",
    tool: str = "mcp__literature__check_quote",
) -> dict:
    return {
        "agent_id": "a-hash",
        "label": label,
        "tool": tool,
        "ordinal": stage_ordinal,
        "at": at,
        "input_preview": json.dumps({"paper": paper, "quote": "a passage"}),
        "status": "ok",
        "result_bytes": 300,
        "result_preview": preview,
    }


def server(**overrides) -> dict:
    row = {
        "ts": "2026-10-10T08:20:05Z",
        "paper_arg": "doi:10.1038/x",
        "paper": "doi:10.1038/x",
        "quote": "a passage",
        "status": "checked",
        "verdict": "exact",
        "cause": None,
    }
    row.update(overrides)
    return row


# --- the three legs -----------------------------------------------------------------------------------------


def test_the_three_logs_are_counted_as_three_legs_and_never_summed(tmp_path):
    """The error the throwaway scripts made: one glob over `literature-calls*.jsonl`."""
    results = archive(
        tmp_path,
        tool={"03-literature": [call(preview='{"status":"checked","verdict":"exact"}')]},
        agents=[server()],
        host=[
            server(status="unverifiable", verdict=None, cause="paper_not_in_store"),
            server(status="unverifiable", verdict=None, cause="text_not_available"),
        ],
        audit=[server(status="unverifiable", verdict=None, cause="text_not_available")],
    )
    data = collect(results)

    assert data["legs"]["agents"]["checks"] == 1
    assert data["legs"]["host"]["checks"] == 2
    assert data["audit_rechecks"]["rows"] == 1
    # The audit's row is in no leg: 1 + 2 is the run's own work, 3 would include a re-check made afterwards.
    assert data["legs"]["agents"]["checks"] + data["legs"]["host"]["checks"] == 3
    assert "after the run" in data["audit_rechecks"]["note"]


def test_the_agents_stage_is_recorded_and_the_hosts_is_inferred(tmp_path):
    results = archive(tmp_path, tool={"03-literature": [call()]}, agents=[server()], host=[server()])
    legs = collect(results)["legs"]

    assert legs["agents"]["stage_source"] == "recorded"
    assert "rule" not in legs["agents"]
    assert legs["host"]["stage_source"] == "inferred_from_timestamps"
    assert "carries no phase" in legs["host"]["rule"]
    assert legs["host"]["rule"] in render(collect(results))


def test_the_two_logs_of_the_agents_leg_are_compared_and_a_disagreement_is_stated(tmp_path):
    """They agreed exactly in every real archive, so a difference is a finding about the archive."""
    agreeing = collect(archive(tmp_path / "a", tool={"03-literature": [call()]}, agents=[server()]))
    assert "disagreement" not in agreeing["legs"]["agents"]

    off = collect(archive(tmp_path / "b", tool={"03-literature": [call(), call(2)]}, agents=[server()]))
    assert "2 check_quote calls" in off["legs"]["agents"]["disagreement"]
    assert off["legs"]["agents"]["server_log_rows"] == 1


# --- the host's inferred stage ------------------------------------------------------------------------------


def test_a_host_row_is_attributed_to_the_last_stage_that_had_started(tmp_path):
    results = archive(
        tmp_path,
        tool={
            "03-literature": [call(at="2026-10-10T08:10:00.000Z")],
            "05-review": [call(at="2026-10-10T08:20:00.000Z")],
        },
        host=[
            server(ts="2026-10-10T08:05:00Z"),  # before any stage ran
            server(ts="2026-10-10T08:15:00Z"),  # in the gap after literature: literature's commit
            server(ts="2026-10-10T08:25:00Z"),
        ],
    )  # after review started
    by_stage = collect(results)["legs"]["host"]["by_stage"]

    assert by_stage["before_any_stage"] == {"checked:exact": 1}
    assert by_stage["03-literature"] == {"checked:exact": 1}
    assert by_stage["05-review"] == {"checked:exact": 1}


def test_the_stage_windows_come_from_the_tool_logs_own_timestamps(tmp_path):
    results = archive(
        tmp_path, tool={"05-review": [call(at="2026-10-10T08:20:00.000Z"), call(2, at="2026-10-10T08:19:00.000Z")]}
    )
    assert stage_windows(results) == [("05-review", "2026-10-10T08:19:00.000Z")]
    assert attribute("2026-10-10T08:00:00Z", stage_windows(results)) == "before_any_stage"


def test_a_host_row_with_no_timestamp_is_named_rather_than_dropped(tmp_path):
    results = archive(tmp_path, tool={"03-literature": [call()]}, host=[server(ts=None)])
    assert "no_timestamp" in collect(results)["legs"]["host"]["by_stage"]


# --- the outcome vocabulary ---------------------------------------------------------------------------------


def test_a_cause_the_truncation_cut_is_counted_unparsed_not_as_a_success(tmp_path):
    """A preview cut before its `cause` must not inflate `checked:exact`, and must be visible on its own."""
    cut = '{"status":"unverifiable","verdict":null,"score":null,"critical_mismatch":null,"passage":nul'
    assert outcome_of_preview(cut) == UNPARSED
    results = archive(tmp_path, tool={"03-literature": [call(preview=cut)]})
    totals = collect(results)["legs"]["agents"]["totals"]
    assert totals == {UNPARSED: 1}
    assert "checked:exact" not in totals


@pytest.mark.parametrize(
    "row, expected",
    [
        ({"status": "checked", "verdict": "exact"}, "checked:exact"),
        ({"status": "checked", "verdict": "near"}, "checked:near"),
        ({"status": "checked", "verdict": "absent"}, "checked:absent"),
        ({"status": "unverifiable", "cause": "paper_not_in_store"}, "paper_not_in_store"),
        ({"status": "unverifiable", "cause": "text_hash_mismatch"}, "text_hash_mismatch"),
        ({"status": "unverifiable", "cause": None}, UNPARSED),
        ({"status": "unverifiable", "cause": "something_new"}, UNPARSED),
        ({"status": "checked", "verdict": None}, UNPARSED),
    ],
)
def test_every_server_row_maps_to_one_label_of_the_vocabulary(row, expected):
    assert outcome_of(row) == expected
    assert expected in OUTCOMES


def test_the_vocabulary_covers_the_producers_own_enums():
    """If `tools/literature/mcp_server.py` gains a cause, this is the test that notices."""
    from tools.literature import mcp_server

    source = Path(mcp_server.__file__).read_text()
    for cause in CAUSES:
        assert f'"{cause}"' in source or f"'{cause}'" in source, f"{cause} is not a cause the server emits"
    assert set(OUTCOMES) == {f"checked:{v}" for v in VERDICTS} | set(CAUSES) | {UNPARSED}


# --- the resolvable dead ends -------------------------------------------------------------------------------


def test_a_refusal_on_a_resolvable_identifier_is_counted_apart_from_one_on_a_url(tmp_path):
    refused = '{"status":"unverifiable","cause":"paper_not_in_store"}'
    results = archive(
        tmp_path,
        tool={
            "05-review": [
                call(1, preview=refused, paper="doi:10.1073/pnas.2614908123"),
                call(2, preview=refused, paper="pmid:23740901"),
                call(3, preview=refused, paper="https://news.uzh.ch/en/articles/x.html"),
            ]
        },
    )
    leg = collect(results)["legs"]["agents"]

    assert leg["totals"]["paper_not_in_store"] == 3
    assert leg["paper_not_in_store_resolvable"] == {"05-review": 2}
    assert leg["paper_not_in_store_resolvable_total"] == 2


def test_the_host_legs_resolvable_count_reads_the_argument_as_given(tmp_path):
    results = archive(
        tmp_path,
        tool={"03-literature": [call()]},
        host=[
            server(
                ts="2026-10-10T08:25:00Z",
                status="unverifiable",
                verdict=None,
                cause="paper_not_in_store",
                paper_arg="doi:10.1038/nature14973",
            ),
            server(
                ts="2026-10-10T08:25:01Z",
                status="unverifiable",
                verdict=None,
                cause="paper_not_in_store",
                paper_arg="Web search results",
            ),
        ],
    )
    assert collect(results)["legs"]["host"]["paper_not_in_store_resolvable_total"] == 1


# --- the fetches --------------------------------------------------------------------------------------------


def test_the_fetch_outcomes_are_read_from_the_phase_reports_on_the_same_stage_axis(tmp_path):
    results = archive(
        tmp_path,
        tool={"03-literature": [call()]},
        fetched={"literature": {"doi:10.1038/a": "fetched", "doi:10.1038/b": "paywalled"}},
    )
    fetch = collect(results)["fetches"]

    assert fetch["by_stage"]["literature"] == {"fetched": 1, "paywalled": 1}
    assert fetch["attempts"] == 2
    assert fetch["not_fetched_identifiers"]["literature"] == [
        {"id": "doi:10.1038/b", "outcome": "paywalled", "reason": None}
    ]
    assert set(FETCH_OUTCOMES) >= set(fetch["totals"])


def test_a_stage_that_attempted_no_fetch_is_absent_from_the_table_and_said_to_be(tmp_path):
    results = archive(tmp_path, tool={"05-review": [call()]}, fetched={})
    data = collect(results)
    assert data["fetches"]["by_stage"] == {} and data["fetches"]["attempts"] == 0
    assert "attempted none" in render(data)


# --- stages, absence, aggregation ---------------------------------------------------------------------------


def test_an_attempt_suffixed_stage_keeps_its_own_entry(tmp_path):
    results = archive(tmp_path, tool={"05-review": [call()], "05-review.attempt2": [call()]})
    by_stage = collect(results)["legs"]["agents"]["by_stage"]

    assert set(by_stage) == {"05-review", "05-review.attempt2"}
    assert phase_of("05-review.attempt2") == "review"
    assert order_stages(["12-synthesize", "05-review.attempt2", "03-literature", "05-review"]) == [
        "03-literature",
        "05-review",
        "05-review.attempt2",
        "12-synthesize",
    ]


def test_an_unrecognised_stage_sorts_last_rather_than_raising():
    assert order_stages(["99-nonesuch", "03-literature"]) == ["03-literature", "99-nonesuch"]


def test_a_stage_with_no_checks_is_reported_as_none_not_as_zero(tmp_path):
    """A phase whose agents called no tool is not an error, and must not appear as a row of zeros."""
    results = archive(tmp_path, tool={"03-literature": [call()], "04-generate": []})
    by_stage = collect(results)["legs"]["agents"]["by_stage"]
    assert set(by_stage) == {"03-literature"}
    assert "04-generate" not in by_stage


def test_an_absent_log_is_distinguished_from_an_empty_one(tmp_path):
    results = archive(tmp_path, host=[], tool={"03-literature": [call()]})
    inputs = collect(results)["inputs"]
    assert inputs["literature-calls.jsonl"] == "empty"
    assert inputs["literature-calls.agents.jsonl"] == "absent"
    assert inputs["literature-calls.audit.jsonl"] == "absent"


def test_the_aggregate_sums_per_leg_and_per_stage_and_names_its_runs(tmp_path):
    refused = '{"status":"unverifiable","cause":"paper_not_in_store"}'
    one = collect(
        archive(
            tmp_path / "one",
            run_id="R1",
            tool={"03-literature": [call(), call(2, preview=refused)]},
            fetched={"literature": {"doi:10.1038/a": "fetched"}},
        )
    )
    two = collect(
        archive(
            tmp_path / "two",
            run_id="R2",
            tool={"03-literature": [call()], "05-review": [call(preview=refused)]},
            fetched={"review": {"doi:10.1038/b": "paywalled"}},
        )
    )
    both = aggregate([one, two])

    assert both["runs"] == ["R1", "R2"]
    assert both["legs"]["agents"]["by_stage"]["03-literature"] == {"checked:exact": 2, "paper_not_in_store": 1}
    assert both["legs"]["agents"]["by_stage"]["05-review"] == {"paper_not_in_store": 1}
    assert both["legs"]["agents"]["checks"] == 4
    assert both["legs"]["agents"]["paper_not_in_store_resolvable_total"] == 2
    assert both["fetches"]["totals"] == {"fetched": 1, "paywalled": 1}
    assert both["legs"]["host"]["stage_source"] == "inferred_from_timestamps"


def test_the_aggregate_states_the_same_limits_as_one_archive(tmp_path):
    one = collect(archive(tmp_path, tool={"03-literature": [call()]}))
    assert aggregate([one])["limits"] == one["limits"]
    assert "the host verifier and the agents are different callers" in one["limits"]["legs_are_not_summed"]


# --- the CLI ------------------------------------------------------------------------------------------------


def test_the_cli_refuses_both_an_archive_and_a_run_list(tmp_path, capsys):
    results = archive(tmp_path, tool={"03-literature": [call()]})
    assert main([str(results), "--runs", "R1"]) == 2
    assert main([]) == 2
    assert "one results directory, or --runs" in capsys.readouterr().err


def test_the_cli_reports_a_run_it_could_not_read_and_fails_only_if_none_was_read(tmp_path, capsys):
    archive(tmp_path / "R1", run_id="R1", tool={"03-literature": [call()]})
    assert main(["--runs", "R1", "R2", "--experiments", str(tmp_path), "--format", "json"]) == 0
    assert "R2: skipped" in capsys.readouterr().err
    assert main(["--runs", "R9", "--experiments", str(tmp_path)]) == 1


def test_the_json_format_carries_the_structure(tmp_path, capsys):
    results = archive(tmp_path, tool={"03-literature": [call()]})
    assert main([str(results), "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert set(data) >= {"run_id", "stages", "legs", "audit_rechecks", "fetches", "inputs", "limits"}


def test_a_missing_archive_is_an_input_error(tmp_path, capsys):
    with pytest.raises(OutcomesInput, match="no such directory"):
        collect(tmp_path / "nowhere")
    assert main([str(tmp_path / "nowhere")]) == 2
    assert "quote_outcomes:" in capsys.readouterr().err


# --- what became of a paper the agent found and could not read ----------------------------------


def snapshot(ts: str, *identifiers: str) -> dict:
    return {
        "ts": ts,
        "event": "server_start",
        "store_snapshot": [
            {"canonical_id": i, "aliases": [i], "status": "full_text", "sha256": "a"} for i in identifiers
        ],
    }


def refusal(ident: str, ts: str) -> dict:
    return server(ts=ts, paper_arg=ident, paper=None, status="unverifiable", verdict=None, cause="paper_not_in_store")


def test_a_paper_that_arrived_after_the_refusal_is_not_the_same_fate_as_one_that_never_came(tmp_path):
    """The two halves of the dead end: the agent was early, or there was no path at all."""
    results = archive(
        tmp_path,
        agents=[
            snapshot("2026-10-10T08:00:00Z"),
            refusal("doi:10.1000/early", "2026-10-10T08:05:00Z"),
            refusal("doi:10.1000/never", "2026-10-10T08:06:00Z"),
            snapshot("2026-10-10T09:00:00Z", "doi:10.1000/early"),
        ],
    )
    data = dead_ends(results)
    assert data["identifiers"] == 2
    assert {r["identifier"]: r["fate"] for r in data["fates"]} == {
        "doi:10.1000/early": LATER,
        "doi:10.1000/never": NEVER_CITED,
    }


def test_a_paper_in_the_store_from_the_start_is_counted_apart_not_as_arrived_later(tmp_path):
    """`paper_not_in_store` on a paper the store already held has another cause — a text whose hash no longer
    matches, say. It must not be read as the agent having been early."""
    results = archive(
        tmp_path,
        agents=[
            snapshot("2026-10-10T08:00:00Z", "doi:10.1000/held"),
            refusal("doi:10.1000/held", "2026-10-10T08:05:00Z"),
        ],
    )
    data = dead_ends(results)
    assert data["in_the_store_from_the_start"] == 1
    assert data["by_fate"] == {NEVER_CITED: 1}


def test_the_fate_distinguishes_a_fetch_that_was_tried_from_one_that_was_never_a_candidate(tmp_path):
    results = archive(
        tmp_path,
        agents=[
            snapshot("2026-10-10T08:00:00Z"),
            refusal("doi:10.1000/tried", "2026-10-10T08:05:00Z"),
            refusal("doi:10.1000/untried", "2026-10-10T08:06:00Z"),
        ],
        fetched={"literature": {"doi:10.1000/tried": {"outcome": "paywalled", "reason": "no open access"}}},
    )
    rows = {r["identifier"]: r for r in dead_ends(results)["fates"]}
    assert rows["doi:10.1000/tried"]["host_fetch_outcome"] == "paywalled"
    assert rows["doi:10.1000/tried"]["fate"] != rows["doi:10.1000/untried"]["fate"]


def test_an_unresolvable_argument_is_not_a_dead_end_of_this_kind(tmp_path):
    """A refusal on a news page or a sentence is a different defect (bug observatory entry 11), not a paper
    the system could have fetched and did not."""
    results = archive(
        tmp_path,
        agents=[
            snapshot("2026-10-10T08:00:00Z"),
            refusal("Web search results", "2026-10-10T08:05:00Z"),
            refusal("https://news.example/als", "2026-10-10T08:06:00Z"),
        ],
    )
    assert dead_ends(results)["identifiers"] == 0


def test_the_five_fates_are_exhaustive_and_named(tmp_path):
    results = archive(
        tmp_path, agents=[snapshot("2026-10-10T08:00:00Z"), refusal("doi:10.1000/x", "2026-10-10T08:05:00Z")]
    )
    assert set(dead_ends(results)["by_fate"]) <= set(FATES)
    assert len(FATES) == 5 and len(set(FATES)) == 5


def test_the_aggregate_keys_dead_ends_on_the_archive_not_the_run_id(tmp_path):
    """A replayed run records the `run_id` of the run it replays; keying on that silently merged its dead ends
    into the other run's and lost 12 of the 151."""
    first = archive(
        tmp_path / "a",
        run_id="SAME",
        agents=[snapshot("2026-10-10T08:00:00Z"), refusal("doi:10.1000/x", "2026-10-10T08:05:00Z")],
    )
    second = archive(
        tmp_path / "b",
        run_id="SAME",
        agents=[snapshot("2026-10-10T08:00:00Z"), refusal("doi:10.1000/x", "2026-10-10T08:05:00Z")],
    )
    data = aggregate([collect(first), collect(second)])
    assert data["dead_ends"]["identifiers"] == 2
    assert data["dead_ends"]["by_fate"] == {NEVER_CITED: 2}


# --- the real archives --------------------------------------------------------------------------------------


@pytest.mark.skipif(
    not (PROD / "pl-prod-010" / "results" / "literature-calls.jsonl").is_file(),
    reason="no archived run with a literature-call log in this checkout",
)
def test_pl_prod_010_reproduces_the_per_stage_reading():
    data = collect(PROD / "pl-prod-010" / "results")

    # The host's inferred stage lands only where the orchestrator commits literature claims.
    assert set(data["legs"]["host"]["by_stage"]) == {"03-literature", "05-review", "06-verify"}
    assert data["legs"]["agents"]["stage_source"] == "recorded"
    assert data["legs"]["agents"]["server_log_rows"] == data["legs"]["agents"]["checks"]
    assert "disagreement" not in data["legs"]["agents"]


@pytest.mark.skipif(
    not all((PROD / r / "results" / "state.json").is_file() for r in RUNS),
    reason="not every archived run is present in this checkout",
)
def test_the_aggregate_reproduces_the_measurement_taken_by_hand():
    """Measured independently before this module existed; recorded to be contradicted."""
    data = aggregate([collect(PROD / r / "results") for r in RUNS])
    agents, host = data["legs"]["agents"], data["legs"]["host"]

    assert (
        agents["checks"],
        agents["totals"]["paper_not_in_store"],
        agents["paper_not_in_store_resolvable_total"],
    ) == (760, 208, 192)
    assert (host["checks"], host["totals"]["paper_not_in_store"], host["paper_not_in_store_resolvable_total"]) == (
        550,
        48,
        25,
    )
    # The two run-time legs are history and cannot move. The audit leg can: `citation_audit` appends to
    # `literature-calls.audit.jsonl` every time it is re-run, and it was re-run to measure the
    # OpenAlex fallback, taking it from 44 rows to 60. So this one is a floor, not an equality.
    assert data["audit_rechecks"]["rows"] >= 44
    assert data["fetches"]["attempts"] == 84 and data["fetches"]["totals"]["fetched"] == 44
    # What became of the 192. A throwaway script measured this first and got two of the five fates wrong, by
    # counting a `not_available` record as the paper having arrived: it read 82/49/12/7/1 and reported that
    # **no** dead end was a paywall. Corrected here: 16 of the 151 were paywalled, which is the part no tool
    # can close. The figures are recorded to be contradicted, not to be trusted.
    dead = data["dead_ends"]
    assert dead["identifiers"] == 151
    assert dead["by_fate"] == {
        "never cited by a claim, so the host never had it as a fetch candidate": 85,
        "arrived in the store later in the same run": 29,
        "the host tried and could not get it": 17,
        "a claim cites it and no fetch was attempted": 13,
        "fetched by the host, but no later agent check saw it": 7,
    }
    outcomes = Counter(r["host_fetch_outcome"] for r in dead["fates"])
    assert outcomes["paywalled"] == 16 and outcomes["failed"] == 1
    # And a paper the host could not get never reads as one that arrived.
    assert all(
        r["fate"] != "arrived in the store later in the same run"
        for r in dead["fates"]
        if r["host_fetch_outcome"] == "paywalled"
    )


def test_the_block_reaches_the_scorecard(tmp_path):
    """`evaluation/figures.py` draws only from `run-score*.json`, so the measurement has to be in it."""
    from evaluation.run_score import quote_outcomes_block

    results = archive(tmp_path, tool={"03-literature": [call()]})
    block = quote_outcomes_block(results)
    assert block["legs"]["agents"]["checks"] == 1
    assert quote_outcomes_block(tmp_path / "nowhere") == {"reason": f"no such directory: {tmp_path / 'nowhere'}"}


def test_the_aggregate_does_not_print_one_runs_row_count_over_many(tmp_path):
    """Found by reading the output: `sources` carried the first archive's counts beside a sum of all."""
    parts = [
        collect(archive(tmp_path / f"r{i}", run_id=f"R{i}", tool={"03-literature": [call()]}, agents=[server()]))
        for i in range(3)
    ]
    sources = aggregate(parts)["legs"]["agents"]["sources"]
    assert all("over 3 archives" in s for s in sources)
    assert not any("(1 " in s or "(3 " in s for s in sources), sources


# --- the fetch reason, and the archives written before it ---------------------------------------------------


def test_the_fetch_reason_separates_europe_pmc_from_openalex(tmp_path):
    """`paywalled` alone cannot be acted on: it covers "Europe PMC has none" and "and neither does OpenAlex"."""
    both = "no open-access full text on Europe PMC; no publishedVersion PDF on OpenAlex"
    results = archive(
        tmp_path,
        tool={"03-literature": [call()]},
        fetched={
            "literature": {
                "pmcid:PMC1": {"outcome": "paywalled", "reason": both},
                "pmcid:PMC2": {"outcome": "paywalled", "reason": "no open-access full text on Europe PMC"},
                "doi:10.1038/x": {"outcome": "fetched", "reason": ""},
            }
        },
    )
    fetch = collect(results)["fetches"]

    assert fetch["totals"] == {"paywalled": 2, "fetched": 1}
    assert fetch["reasons"] == {both: 1, "no open-access full text on Europe PMC": 1}
    assert fetch["reason_unrecorded"] == 0
    assert both in render(collect(results))


def test_an_archive_written_before_the_reason_was_kept_still_reads(tmp_path):
    """14 archives carry the bare outcome string; their reason is unrecorded, never invented."""
    results = archive(
        tmp_path,
        tool={"03-literature": [call()]},
        fetched={"literature": {"pmcid:PMC1": "paywalled", "doi:10.1038/x": "fetched"}},
    )
    fetch = collect(results)["fetches"]

    assert fetch["totals"] == {"paywalled": 1, "fetched": 1}
    assert fetch["reasons"] == {} and fetch["reason_unrecorded"] == 1
    assert "cannot be split between Europe PMC and OpenAlex" in render(collect(results))


def test_the_two_shapes_count_the_same_outcome(tmp_path):
    from pipeline.provenance import fetch_outcome

    assert fetch_outcome("paywalled") == ("paywalled", "")
    assert fetch_outcome({"outcome": "paywalled", "reason": "x"}) == ("paywalled", "x")
    assert fetch_outcome({"outcome": "fetched"}) == ("fetched", "")
