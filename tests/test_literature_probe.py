"""`literature` in isolation, on the cases of `tests/fixtures/literature/`.

The mocked tests run always. `test_a_live_literature_phase_fetches_a_paper_it_identified` launches a real agent
and runs only with `ALS_LIVE_LITERATURE=1` (paid): it is the only way to establish a fact about an agent's
behaviour — whether, told it may fetch a paper it has identified, it does. Every offline test of `fetch_paper`
answers whether the tool works, not whether an agent uses it.
"""

from __future__ import annotations

import datetime
import json
import os
from pathlib import Path

import pytest

from pipeline.config import DEFAULT_CONFIG, load_config
from pipeline.launcher import FakeLauncher
from pipeline.literature_probe import (
    AGENT_FETCH,
    DEFAULT_MAX_COST_USD,
    InvalidDelivery,
    check_case,
    load_case,
    prepare,
    probe_config,
    readout,
    run,
)

FIXTURE = Path(__file__).parent / "fixtures" / "literature"
CASES = sorted((FIXTURE / "cases").glob("*.json"))
LIVE = os.environ.get("ALS_LIVE_LITERATURE") == "1"
QUOTE = "C9orf72-specific CD4+ T cell responses were increased in patients with ALS"


def now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def test_the_fixture_holds_the_case_the_probe_exists_for() -> None:
    assert {c.stem for c in CASES} >= {"fetch_a_paper_it_found"}


@pytest.mark.parametrize("path", CASES, ids=lambda p: p.stem)
def test_every_case_is_runnable(path: Path) -> None:
    assert check_case(load_case(path)) == []


@pytest.mark.parametrize("field, reason", [("questions", "names no question"), ("run_id", "names no run id")])
def test_a_case_that_could_not_run_is_refused_before_anything_is_written(tmp_path, field, reason) -> None:
    case = load_case(CASES[0])
    broken = type(case)(
        case.name, "" if field == "run_id" else case.run_id, case.kb, () if field == "questions" else case.questions
    )
    with pytest.raises(InvalidDelivery, match=reason):
        prepare(broken, tmp_path, now())
    assert not list(tmp_path.iterdir())


def test_the_store_the_probe_creates_is_empty_so_the_tool_is_the_only_route(tmp_path) -> None:
    """The whole design of the probe: with a paper already in the store the agent could verify a quote without
    fetching, and the run would prove nothing about the behaviour under test."""
    probe = prepare(load_case(CASES[0]), tmp_path, now())
    assert (probe.store / "index.jsonl").read_text() == ""
    assert probe.run_dir.is_dir() and probe.kb_root.is_dir()


def test_the_probe_grants_a_fetch_budget_and_its_own_store_without_touching_the_production_questions(tmp_path):
    """The model and the prompt are not replaced — they are what is tested — but the store, the questions, the
    budget and the cap are the probe's, so no production store is written."""
    case = load_case(CASES[0])
    probe = prepare(case, tmp_path, now())
    config = probe_config(DEFAULT_CONFIG, case, probe.store, 1.5)
    assert config.phases.literature.questions == list(case.questions)
    assert config.phases.literature.agent_fetch_max_new == AGENT_FETCH
    assert config.store_path() == probe.store and config.run_max_cost_usd == 1.5
    assert config.phases.literature.model == load_config(DEFAULT_CONFIG).phases.literature.model


def test_a_probe_never_overwrites_another(tmp_path) -> None:
    case = load_case(CASES[0])
    at = now()
    prepare(case, tmp_path, at)
    with pytest.raises(FileExistsError):
        prepare(case, tmp_path, at)


def test_the_readout_names_what_the_probe_is_for(tmp_path) -> None:
    """A fake agent that writes one claim and one fetch row: the readout must separate "an agent fetched a
    paper" from "the host did", because only the first is the behaviour under test."""
    case = load_case(CASES[0])
    probe = prepare(case, tmp_path, now())
    (probe.run_dir / "literature-calls.agents.jsonl").write_text(
        json.dumps({"event": "fetch", "outcome": "fetched", "retries": 2, "identifier": "doi:10.1000/x"})
        + "\n"
        + json.dumps({"ts": "t", "quote": QUOTE, "status": "checked", "verdict": "exact"})
        + "\n"
    )
    (probe.run_dir / "01-literature-report.json").write_text(
        json.dumps({"fetched": {"doi:10.1000/y": {"outcome": "paywalled", "retries": 0}}, "written": []})
    )
    out = readout(probe)
    assert (out["agent_fetch_calls"], out["agent_papers_fetched"], out["agent_fetch_retries"]) == (1, 1, 2)
    assert out["agent_quote_checks"] == 1 and out["agent_checks_exact"] == 1
    assert out["host_fetches"] == {"doi:10.1000/y": {"outcome": "paywalled", "retries": 0}}
    assert out["claims_written"] == 0 and out["papers_in_store_now"] == 0


def test_a_mocked_literature_phase_runs_in_the_probe_directory(tmp_path) -> None:
    """The plumbing, with no agent and no network: the phase writes its report and its claims into the probe's
    own directories, and the production store is never opened."""
    case = load_case(CASES[0])
    probe = prepare(case, tmp_path, now())

    def literature(args):
        assert args["questions"] == list(case.questions)
        assert args["agent_fetch"] == AGENT_FETCH, "the prompt must be told the budget it was granted"
        return {
            "phase": "literature",
            "dropped": [],
            "outputs": [
                {
                    "agent_id": "literature-1",
                    "output": {
                        "claims": [
                            {
                                "ref": "c1",
                                "type": "observational",
                                "content": "CD4 responses to C9orf72 are increased.",
                                "source": "doi:10.1000/fixture",
                                "quote": QUOTE,
                            }
                        ]
                    },
                }
            ],
        }

    report = run(
        probe,
        probe_config(DEFAULT_CONFIG, case, probe.store, DEFAULT_MAX_COST_USD),
        launcher_factory=lambda config, kb_root: FakeLauncher({"literature": literature}),
    )
    assert report["written"], report
    assert (probe.run_dir / "01-literature-report.json").is_file()
    out = readout(probe)
    # Unverified: the store is empty, so the quote could not be checked. That is the state the live probe is
    # meant to get past, and the mocked run must not look as if it already had.
    assert out["claims_written"] == 1 and out["claims_verified"] == 0


@pytest.mark.skipif(not LIVE, reason="paid: set ALS_LIVE_LITERATURE=1 to launch a live literature phase")
@pytest.mark.parametrize("path", CASES, ids=lambda p: p.stem)
def test_a_live_literature_phase_fetches_a_paper_it_identified(tmp_path, path: Path) -> None:
    """The one question no offline test can answer: does a real agent, on the real prompt, with an empty store,
    use `fetch_paper` and end with a verified quote? The 85 archived dead ends are what happens when it does
    not. Real agents, real MCP servers, real Europe PMC; the store, the knowledge base, the questions and the
    cap are the probe's. The archive stays under `tmp_path`."""
    case = load_case(path)
    probe = prepare(case, tmp_path, now())
    run(probe, probe_config(DEFAULT_CONFIG, case, probe.store, DEFAULT_MAX_COST_USD))
    out = readout(probe)
    assert out["agent_fetch_calls"] > 0, f"no agent called fetch_paper: {out}"
    assert out["agent_papers_fetched"] > 0, f"no paper arrived: {out}"
    assert out["claims_verified"] > 0, f"a paper arrived but no quote verified against it: {out}"
