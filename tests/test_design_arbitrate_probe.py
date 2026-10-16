"""`design` then `arbitrate`, isolated, on a case offering only one registered handler's data.

Free tests exercise the deterministic plumbing with `FakeLauncher`: a fixed design/arbitrate output is
committed correctly, and `outcome()` reads it back faithfully. The live test (`ALS_LIVE_DESIGN_ARBITRATE=1`)
runs real agents and only reports what they actually did -- it is not allowed to assert a specific choice,
because no fixture can force one (see the module docstring and `docs/pipeline.md`).
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
from pathlib import Path

import pytest

from pipeline.config import DEFAULT_CONFIG
from pipeline.design_arbitrate_probe import (
    DEFAULT_MAX_COST_USD,
    InvalidCase,
    load_case,
    prepare,
    probe_config,
    run,
)
from pipeline.launcher import FakeLauncher

FIXTURE = Path(__file__).parent / "fixtures" / "design_arbitrate"
CASES = sorted((FIXTURE / "cases").glob("*.json"))
LIVE = os.environ.get("ALS_LIVE_DESIGN_ARBITRATE") == "1"


def now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def digests(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def test_the_fixture_holds_the_ics_only_case():
    assert {c.stem for c in CASES} >= {"ics_cytokine_skew_v1_only_data"}


def fake_design_proposing(analysis_id: str | None, dataset_refs: list[str] | None = None):
    def design(args):
        item = args["items"][0]
        target = item["targets"][0]
        exp = {
            "title": "ICS cytokine skew re-analysis",
            "protocol": "Recompute CD4+ cytokine responses from the deposited FlowJo gates.",
            "tested_claims": [target],
            "expected_outcomes": [
                {"outcome": "ALS ratio lower, p<0.05", "claim_id": target, "effect": "supports"},
                {"outcome": "no difference", "claim_id": target, "effect": "contradicts"},
            ],
            "cost": "low",
            "cost_rationale": "two local files",
            "feasibility": "high",
            "kind": "in_silico",
            "dataset_location": "local",
            "dataset_refs": dataset_refs or ["dataset:michaelis-zenodo-cytometry", "dataset:michaelis-source-data"],
        }
        if analysis_id:
            exp["analysis_id"] = analysis_id
        return {
            "outputs": [
                {"agent_id": "designer-1", "hypothesis_id": item["hypothesis_id"], "output": {"experiments": [exp]}}
            ]
        }

    return design


def fake_arbitrate_buying_every_exp(args):
    item = args["items"][0]
    eids = [c["id"] for c in item["context"] if c["id"].startswith("EXP-")]
    return {
        "outputs": [
            {
                "agent_id": f"arbiter@{item['hypothesis_id']}",
                "hypothesis_id": item["hypothesis_id"],
                "output": {
                    "decision": "buy",
                    "experiment_ids": eids,
                    "value_of_information": "high",
                    "rationale": "settles the cytokine-skew claim",
                },
            }
        ]
    }


def fake_arbitrate_stopping(args):
    item = args["items"][0]
    return {
        "outputs": [
            {
                "agent_id": f"arbiter@{item['hypothesis_id']}",
                "hypothesis_id": item["hypothesis_id"],
                "output": {
                    "decision": "stop",
                    "experiment_ids": [],
                    "value_of_information": "low",
                    "rationale": "not worth testing yet",
                },
            }
        ]
    }


@pytest.mark.parametrize("path", CASES, ids=lambda p: p.stem)
def test_a_proposal_naming_the_handler_with_both_its_sources_is_bought(tmp_path, path):
    case = load_case(path)
    before = digests(FIXTURE)
    probe = prepare(case, tmp_path, now())
    report = run(
        probe,
        probe_config(DEFAULT_CONFIG, DEFAULT_MAX_COST_USD),
        launcher_factory=lambda config, kb_root: FakeLauncher(
            {"design": fake_design_proposing("ics_cytokine_skew_v1"), "arbitrate": fake_arbitrate_buying_every_exp}
        ),
    )

    assert report["outcome"]["proposed_experiment_ids"] == ["EXP-1"]
    assert report["outcome"]["data_supported_experiment_ids"] == ["EXP-1"]
    assert report["outcome"]["bought_experiment_ids"] == ["EXP-1"]
    assert report["outcome"]["handler_bought"] is True
    assert digests(FIXTURE) == before
    assert (probe.run_dir / "data-manifest.json").is_file()
    assert json.loads((probe.run_dir / "probe.json").read_text())["run_max_cost_usd"] == DEFAULT_MAX_COST_USD


def test_a_proposal_missing_one_of_the_two_sources_is_not_data_supported(tmp_path):
    """The manifest only has the two ics_cytokine_skew_v1 sources; naming just one of them is unverified, not
    bought -- `select()`/`assess()` require exactly the handler's declared inputs."""
    case = load_case(FIXTURE / "cases" / "ics_cytokine_skew_v1_only_data.json")
    probe = prepare(case, tmp_path, now())
    report = run(
        probe,
        probe_config(DEFAULT_CONFIG, DEFAULT_MAX_COST_USD),
        launcher_factory=lambda config, kb_root: FakeLauncher(
            {
                "design": fake_design_proposing(
                    "ics_cytokine_skew_v1", dataset_refs=["dataset:michaelis-zenodo-cytometry"]
                ),
                "arbitrate": fake_arbitrate_buying_every_exp,
            }
        ),
    )

    assert report["outcome"]["proposed_experiment_ids"] == ["EXP-1"]
    assert report["outcome"]["data_supported_experiment_ids"] == []
    assert report["outcome"]["handler_bought"] is False


def test_a_design_agent_that_proposes_nothing_in_silico_is_reported_honestly(tmp_path):
    """No `Experiment` is committed at all: `design` records `no_experiment_reason`, not a refusal."""
    case = load_case(FIXTURE / "cases" / "ics_cytokine_skew_v1_only_data.json")
    probe = prepare(case, tmp_path, now())

    def no_experiment(args):
        item = args["items"][0]
        return {
            "outputs": [
                {
                    "agent_id": "designer-1",
                    "hypothesis_id": item["hypothesis_id"],
                    "output": {"experiments": [], "no_experiment_reason": "no tractable test found"},
                }
            ]
        }

    report = run(
        probe,
        probe_config(DEFAULT_CONFIG, DEFAULT_MAX_COST_USD),
        launcher_factory=lambda config, kb_root: FakeLauncher(
            {"design": no_experiment, "arbitrate": fake_arbitrate_buying_every_exp}
        ),
    )

    assert report["outcome"] == {
        "proposed_experiment_ids": [],
        "data_supported_experiment_ids": [],
        "bought_experiment_ids": [],
        "handler_bought": False,
        "decisions": [],
    }


def test_an_arbiter_that_stops_is_reported_as_not_bought_even_when_data_supported(tmp_path):
    case = load_case(FIXTURE / "cases" / "ics_cytokine_skew_v1_only_data.json")
    probe = prepare(case, tmp_path, now())
    report = run(
        probe,
        probe_config(DEFAULT_CONFIG, DEFAULT_MAX_COST_USD),
        launcher_factory=lambda config, kb_root: FakeLauncher(
            {"design": fake_design_proposing("ics_cytokine_skew_v1"), "arbitrate": fake_arbitrate_stopping}
        ),
    )

    assert report["outcome"]["data_supported_experiment_ids"] == ["EXP-1"]
    assert report["outcome"]["bought_experiment_ids"] == []
    assert report["outcome"]["handler_bought"] is False
    assert report["outcome"]["decisions"] == [{"id": "D-1", "decision": "stop", "experiment_ids": []}]


def test_a_missing_source_is_refused_before_anything_is_written(tmp_path):
    from tools.data.local_sources import LocalSource

    case = load_case(FIXTURE / "cases" / "ics_cytokine_skew_v1_only_data.json")
    missing = LocalSource(
        id=case.sources[0].id, path=tmp_path / "absent", kind=case.sources[0].kind, license_note="fixture case"
    )
    broken = type(case)(case.name, case.run_id, case.kb, (missing, case.sources[1]))
    with pytest.raises(InvalidCase):
        prepare(broken, tmp_path, now())
    assert not list(tmp_path.iterdir())


def test_a_probe_never_overwrites_another(tmp_path):
    case = load_case(FIXTURE / "cases" / "ics_cytokine_skew_v1_only_data.json")
    at = now()
    prepare(case, tmp_path, at)
    with pytest.raises(FileExistsError):
        prepare(case, tmp_path, at)


@pytest.mark.skipif(not LIVE, reason="paid: set ALS_LIVE_DESIGN_ARBITRATE=1 to run real design+arbitrate agents")
@pytest.mark.parametrize("path", CASES, ids=lambda p: p.stem)
def test_a_live_design_and_arbitrate_report_their_real_choice(tmp_path, path):
    """No assertion forces a specific choice -- see the module docstring. This prints and checks only what must
    be true by construction: if the handler was proposed and resolved data_supported, arbitrate's own context
    for this hypothesis names it, whatever it then decided to do with it."""
    case = load_case(path)
    probe = prepare(case, tmp_path, now())
    report = run(probe, probe_config(DEFAULT_CONFIG, DEFAULT_MAX_COST_USD))
    print(
        json.dumps(
            {
                "case": case.name,
                "outcome": report["outcome"],
                "design_no_experiment": report["design"].get("no_experiment"),
                "design_cost_usd": (report["design"].get("launch") or {}).get("session_cost_usd"),
                "arbitrate_cost_usd": (report["arbitrate"].get("launch") or {}).get("session_cost_usd"),
                "results": str(probe.run_dir),
            },
            indent=2,
        )
    )
    out = report["outcome"]
    assert set(out["data_supported_experiment_ids"]) <= set(out["proposed_experiment_ids"])
    assert out["handler_bought"] == bool(set(out["data_supported_experiment_ids"]) & set(out["bought_experiment_ids"]))
