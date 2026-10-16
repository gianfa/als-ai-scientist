"""`reassess` in isolation, on the delivery cases of `tests/fixtures/reassess/`.

Every case names a minimal knowledge base, an artifact and an execute report, so nothing here reads an archived run
or the shared store. The mocked tests run always; `test_a_live_reassessor_judges_each_case` launches a real agent
per case and runs only with `ALS_LIVE_REASSESS=1` (paid, about 0.40 USD a case).
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from pathlib import Path

import pytest
from kb_factories import NOW

from knowledge_base.store import read_records
from pipeline.config import DEFAULT_CONFIG
from pipeline.launcher import FakeLauncher
from pipeline.reassess_probe import (
    DEFAULT_MAX_COST_USD,
    Case,
    InvalidDelivery,
    check_delivery,
    load_case,
    prepare,
    probe_config,
    run,
)

FIXTURE = Path(__file__).parent / "fixtures" / "reassess"
CASES = sorted((FIXTURE / "cases").glob("*.json"))
LIVE = os.environ.get("ALS_LIVE_REASSESS") == "1"


def digests(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def with_report(case: Case, **overrides) -> Case:
    report = copy.deepcopy(case.execute_report) | overrides
    return Case(case.name, case.run_id, case.kb, case.artifacts, report)


def with_execution(case: Case, **overrides) -> Case:
    """Patch the case's one execution row. A report now carries one row per executed EXP, so what
    used to be a top-level field of the report is a field of its execution."""
    report = copy.deepcopy(case.execute_report)
    report["executions"][0] |= overrides
    return Case(case.name, case.run_id, case.kb, case.artifacts, report)


def test_the_fixture_holds_the_two_shapes_seen_live():
    assert {c.stem for c in CASES} >= {"first_execution", "repeated_execution"}


@pytest.mark.parametrize("path", CASES, ids=lambda p: p.stem)
def test_every_case_is_a_valid_delivery(path):
    case = load_case(path)
    assert check_delivery(case.execute_report, read_records(case.kb), case.run_id) == []


@pytest.mark.parametrize(
    "overrides, reason",
    [
        (
            {"result_digest": "7d531cfe240d24ff04a2920f71ab740679ee0f37ebfde6e696229c0c6fed41c1 (outcome: met)"},
            "bare 64-character",
        ),
        ({"result_digest": "a" * 64}, "differs from the digest the interpretation check verified"),
        ({"seen_again": {"C-9": "C-404"}}, "C-404 is not a claim"),
        ({"seen_again": {"C-2": "C-2"}}, "C-2 is in the knowledge base"),
        ({"seen_again": {}}, "holds no claim"),
        ({"hypothesis_id": "H-7"}, "H-7' is not a hypothesis"),
    ],
)
def test_what_reassess_could_not_judge_is_refused_with_its_reason(overrides, reason):
    case = with_execution(load_case(FIXTURE / "cases" / "repeated_execution.json"), **overrides)
    reasons = check_delivery(case.execute_report, read_records(case.kb), case.run_id)
    assert any(reason in r for r in reasons), reasons


@pytest.mark.parametrize(
    "overrides, reason",
    [
        ({"status": "not_executed"}, "not 'executed'"),
        ({"executions": []}, "non-empty list of execution rows"),
    ],
)
def test_a_report_with_no_executed_row_is_refused(overrides, reason):
    case = with_report(load_case(FIXTURE / "cases" / "repeated_execution.json"), **overrides)
    reasons = check_delivery(case.execute_report, read_records(case.kb), case.run_id)
    assert any(reason in r for r in reasons), reasons


def test_one_broken_row_among_good_ones_is_not_a_delivery():
    """Two executions and one of them unjudgeable is not a delivery: a probe that passed would test half a
    phase and report a success."""
    case = load_case(FIXTURE / "cases" / "repeated_execution.json")
    report = copy.deepcopy(case.execute_report)
    report["executions"].append(report["executions"][0] | {"experiment_id": "EXP-404"})
    report["executed_count"] = 2
    reasons = check_delivery(report, read_records(case.kb), case.run_id)
    assert reasons == ["EXP-404: selected experiment 'EXP-404' is not an experiment in the knowledge base"]


def test_a_hypothesis_of_another_run_is_refused():
    """`reassess` reads only the hypotheses of the run it is in, so the probe runs under the case's run ID."""
    case = load_case(FIXTURE / "cases" / "repeated_execution.json")
    reasons = check_delivery(case.execute_report, read_records(case.kb), "RUN-2")
    assert reasons == [f"EXP-1: hypothesis H-1 belongs to run {case.run_id}, not RUN-2"]


def fake_reassessor(shown: list[dict]):
    """One critic per executed EXP, labelled as the workflow labels them: the host routes by that label."""

    def reassess(args):
        shown.append(args)
        outputs = []
        for entry in args["executions"]:
            execution = entry["execution"]
            outputs.append(
                {
                    "agent_id": f"reassessor@{execution['experiment_id']}",
                    "output": {
                        "experiment_id": execution["experiment_id"],
                        "result_digest": execution["result_digest"],
                        "decision": "inconclusive",
                        "rationale": "One panel cannot settle the claim.",
                        "claim_ids": execution["written"] or execution["seen_again"],
                        "limitations": ["No allele frequencies."],
                    },
                }
            )
        return {"outputs": outputs}

    return reassess


@pytest.mark.parametrize("path", CASES, ids=lambda p: p.stem)
def test_each_case_is_judged_in_its_own_directory_and_the_fixture_is_untouched(tmp_path, path):
    case = load_case(path)
    before = digests(FIXTURE)
    shown: list[dict] = []
    probe = prepare(case, tmp_path, NOW)
    report = run(
        probe,
        probe_config(DEFAULT_CONFIG, DEFAULT_MAX_COST_USD),
        launcher_factory=lambda config, kb_root: FakeLauncher({"reassess": fake_reassessor(shown)}),
    )

    assert report["status"] == "judged", report["judgements"]
    row = case.execute_report["executions"][0]
    result_claims = [*row["written"], *row["seen_again"].values()]
    (judgement,) = report["judgements"]
    assert judgement["status"] == "judged", judgement.get("error")
    assert set(judgement["judgement"]["claim_ids"]) == set(result_claims)
    hypothesis = shown[0]["executions"][0]["hypothesis"]
    assert sorted(c["id"] for c in hypothesis["computed_result_claims"]) == sorted(result_claims)
    dropped = set(row["seen_again"])
    assert not dropped & set(re.findall(r"C-\d+", json.dumps(shown[0]))), "a dropped ID reached the agent"
    assert digests(FIXTURE) == before
    assert (probe.run_dir / "experiment-analysis" / "EXP-1" / "summary.json").is_file()
    assert json.loads((probe.run_dir / "probe.json").read_text())["run_max_cost_usd"] == DEFAULT_MAX_COST_USD


def test_an_invalid_case_writes_nothing(tmp_path):
    case = with_report(load_case(FIXTURE / "cases" / "repeated_execution.json"), status="not_executed")
    with pytest.raises(InvalidDelivery, match="not 'executed'"):
        prepare(case, tmp_path, NOW)
    assert not list(tmp_path.iterdir())


def test_a_probe_never_overwrites_another(tmp_path):
    case = load_case(FIXTURE / "cases" / "repeated_execution.json")
    prepare(case, tmp_path, NOW)
    with pytest.raises(FileExistsError):
        prepare(case, tmp_path, NOW)


@pytest.mark.skipif(not LIVE, reason="paid: set ALS_LIVE_REASSESS=1 to launch a live reassessor per case")
@pytest.mark.parametrize("path", CASES, ids=lambda p: p.stem)
def test_a_live_reassessor_judges_each_case(tmp_path, path):
    """The agent, its prompt and its contract, on an input that is known to be valid: a failure here is the
    reassessor's, not the delivery's. The archive of the probe stays under `tmp_path` for inspection."""
    case = load_case(path)
    probe = prepare(case, tmp_path, NOW)
    report = run(probe, probe_config(DEFAULT_CONFIG, DEFAULT_MAX_COST_USD))
    print(
        json.dumps(
            {
                "case": case.name,
                "status": report.get("status"),
                "error": report.get("error"),
                "judgement": report.get("judgement"),
                "results": str(probe.run_dir),
                "cost_usd": (report.get("launch") or {}).get("session_cost_usd"),
            },
            indent=2,
        )
    )
    assert report["status"] == "judged", report.get("error")
