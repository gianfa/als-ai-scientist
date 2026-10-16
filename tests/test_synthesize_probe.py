"""`synthesize` in isolation, after a real `execute`, on the case of `tests/fixtures/synthesize/`.

The archive is built by the phases themselves: the real handler writes `EXP-1`'s artifact and receipt and the host
binds `EXP-2` to it as a reuse. Only the agents are scripted. The regression this guards: synthesize looked for a
receipt under the reuse's own ID.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
from pathlib import Path

import pytest

from pipeline.config import DEFAULT_CONFIG
from pipeline.execute_probe import InvalidDelivery, load_case
from pipeline.launcher import ClaudeLauncher
from pipeline.synthesize_probe import (
    DEFAULT_LIVE_MAX_COST_USD,
    DEFAULT_MAX_COST_USD,
    _Scripted,
    check_selection,
    main,
    prepare,
    probe_config,
    run,
    scripted_executor,
    scripted_pi_output,
    verdict,
)
from tools.data.local_sources import manifest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).parent / "fixtures" / "synthesize"
CASE = FIXTURE / "cases" / "reuse_receipt.json"
LIVE = os.environ.get("ALS_LIVE_SYNTHESIZE") == "1"


def now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def digests(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def probed(tmp_path: Path):
    probe = prepare(load_case(CASE), tmp_path, now())
    state = run(
        probe,
        probe_config(DEFAULT_CONFIG, DEFAULT_MAX_COST_USD),
        scripted_executor(probe.run_dir),
        _Scripted({"synthesize": scripted_pi_output}),
    )
    return probe, state


def test_the_case_selects_its_experiment_as_a_reuse():
    case = load_case(CASE)
    assert check_selection(case, manifest(case.sources, ROOT)) == []


def test_a_computation_is_not_a_reuse_and_is_refused():
    case = load_case(CASE)
    first = type(case)(case.name, case.run_id, case.kb, "EXP-1", case.sources)
    assert "not a reuse" in check_selection(first, manifest(case.sources, ROOT))[0]


def test_an_invalid_case_writes_nothing(tmp_path):
    case = load_case(CASE)
    broken = type(case)(case.name, case.run_id, case.kb, "EXP-404", case.sources)
    with pytest.raises(InvalidDelivery, match="EXP-404"):
        prepare(broken, tmp_path, now())
    assert not list(tmp_path.iterdir())


def test_a_reuse_reaches_a_written_proposal_through_its_runners_receipt(tmp_path):
    before = digests(FIXTURE)
    probe, state = probed(tmp_path)

    rows = {r["experiment_id"]: r for r in state["phases"]["execute"]["executions"]}
    assert rows["EXP-1"]["status"] == "executed" and not rows["EXP-1"].get("result_of")
    assert rows["EXP-2"]["status"] == "executed" and rows["EXP-2"]["result_of"] == "EXP-1"
    analysis = probe.run_dir / "experiment-analysis"
    assert (analysis / "EXP-1" / "verification.json").is_file()
    assert not (analysis / "EXP-2").exists(), "a reuse writes no artifact and no receipt of its own"

    proposal = json.loads((probe.run_dir / "final-proposal.json").read_text())
    assert proposal["gate"]["passed"], proposal["gate"]
    assert {r["experiment_id"] for r in proposal["execution"]["executions"]} == {"EXP-1", "EXP-2"}
    assert (probe.run_dir / "final-proposal.md").is_file()
    assert digests(FIXTURE) == before


def test_a_tampered_receipt_still_stops_synthesize(tmp_path):
    probe = prepare(load_case(CASE), tmp_path, now())
    executor = scripted_executor(probe.run_dir)

    class Tampering:
        def run(self, phase, step, args, run_dir):
            outcome = executor.run(phase, step, args, run_dir)
            path = run_dir / "experiment-analysis" / "EXP-1" / "verification.json"
            receipt = json.loads(path.read_text())
            receipt["verified"] = False
            path.write_text(json.dumps(receipt))
            return outcome

    with pytest.raises(Exception, match="digest mismatch|failed check"):
        run(
            probe,
            probe_config(DEFAULT_CONFIG, DEFAULT_MAX_COST_USD),
            Tampering(),
            _Scripted({"synthesize": scripted_pi_output}),
        )
    assert not (probe.run_dir / "final-proposal.json").exists()


def test_the_verdict_reports_the_proposal_and_every_execution(tmp_path):
    probe, state = probed(tmp_path)
    result = verdict(probe, state)
    assert result["proposal_written"] and result["gate"]["passed"]
    assert {k: (v["status"], v["result_of"], v["faithful"]) for k, v in result["executions"].items()} == {
        "EXP-1": ("executed", None, True),
        "EXP-2": ("executed", "EXP-1", True),
    }
    assert not result["synthesize"]["invalid_outputs"]
    assert set(result["cost_usd"]) == {"execute", "synthesize"}


def test_the_command_line_exits_zero_on_the_scripted_case(tmp_path, capsys):
    assert main([str(CASE), "--out", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["proposal_written"] is True
    assert len(list(tmp_path.glob("probe-synthesize-*"))) == 1


@pytest.mark.skipif(not LIVE, reason="paid: set ALS_LIVE_SYNTHESIZE=1 to launch a live executor and a live PI")
def test_live_agents_carry_a_reuse_to_a_written_proposal(tmp_path):
    """The real executor and the real PI, with their prompts, schemas and tools, on the case's archive. The gate's
    verdict is printed and not asserted: a PI failing it is a finding about the PI, not about the host."""
    probe = prepare(load_case(CASE), tmp_path, now())
    config = probe_config(DEFAULT_CONFIG, DEFAULT_LIVE_MAX_COST_USD)
    state = run(probe, config, ClaudeLauncher(config, probe.kb_root), ClaudeLauncher(config, probe.kb_root))
    result = verdict(probe, state)
    print(json.dumps(result, indent=2))
    assert result["proposal_written"], result["synthesize"]
    assert {k: (v["status"], v["result_of"]) for k, v in result["executions"].items()} == {
        "EXP-1": ("executed", None),
        "EXP-2": ("executed", "EXP-1"),
    }
    assert result["executions"]["EXP-1"]["faithful"], result["executions"]["EXP-1"]["errors"]
