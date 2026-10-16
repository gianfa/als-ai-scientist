"""`intervene` in isolation together with the drug lookup it feeds, on the cases of `tests/fixtures/intervene/`.

The mocked tests run always and cover the plumbing: the probe refuses a case `intervene` would be given no
hypothesis for, writes into its own directory, leaves the fixture alone, and runs the lookup with the probe's
own cache rather than the repository's. `test_a_live_agent_names_a_compound_and_the_service_answers` runs only
with `ALS_LIVE_INTERVENE_DRUG_LOOKUP=1` (paid, and it queries MyChem.info over the network): it is the only
test that can show a real agent filling `drug_name` at all, which the prompt asks for only when the agent is
sure of a compound.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
from pathlib import Path

import pytest
from test_pipeline_orchestrator import intervene

from knowledge_base.schemas import Claim, Relation
from knowledge_base.store import read_records
from pipeline import orchestrator as orch_module
from pipeline.config import DEFAULT_CONFIG
from pipeline.intervene_drug_lookup_probe import (
    DEFAULT_MAX_COST_USD,
    REPORT,
    Case,
    InvalidDelivery,
    load_case,
    outcome,
    prepare,
    probe_config,
    run,
    succeeded,
    surviving,
)
from pipeline.launcher import FakeLauncher
from tools.data.pharmacology import DrugRecord

FIXTURE = Path(__file__).parent / "fixtures" / "intervene"
CASES = sorted((FIXTURE / "cases").glob("*.json"))
LIVE = os.environ.get("ALS_LIVE_INTERVENE_DRUG_LOOKUP") == "1"

URL = "https://mychem.info/v1/query?q=fake"
FOUND = DrugRecord(
    query="metformin",
    drugbank_id="DB00331",
    chembl_id="CHEMBL1431",
    chebi_id=None,
    pubchem_cid=None,
    unii=None,
    tradename="metformin",
    retrieved_at="2026-10-10T00:00:00+00:00",
    source_url=URL,
)


def now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def config():
    return probe_config(DEFAULT_CONFIG, DEFAULT_MAX_COST_USD)


def digests(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def named(name: str | None):
    """An `intervene` fake whose single candidate carries `name` as its `drug_name` (None: the agent gave none)."""

    def launch(args):
        out = intervene(args)
        out["outputs"][0]["output"]["candidates"][0]["drug_name"] = name
        return out

    return {"intervene": launch}


def test_the_fixture_holds_the_surviving_hypothesis_case():
    assert {c.stem for c in CASES} == {"surviving_hypothesis"}


@pytest.mark.parametrize("path", CASES, ids=lambda p: p.stem)
def test_every_case_offers_intervene_a_hypothesis(path):
    [item] = surviving(load_case(path), config())
    assert item["targets"], "a candidate needs a claim to act on"


def test_a_run_id_the_arbiter_never_judged_is_refused(tmp_path):
    case = load_case(CASES[0])
    other = Case(case.name, "RUN-NO-SUCH", case.kb)
    assert surviving(other, config()) == []
    with pytest.raises(InvalidDelivery, match="RUN-NO-SUCH"):
        prepare(other, tmp_path, now(), config())
    assert not list(tmp_path.iterdir())


def test_a_probe_never_overwrites_another(tmp_path):
    case, at = load_case(CASES[0]), now()
    prepare(case, tmp_path, at, config())
    with pytest.raises(FileExistsError):
        prepare(case, tmp_path, at, config())


@pytest.mark.parametrize("path", CASES, ids=lambda p: p.stem)
def test_the_lookup_runs_in_the_probes_own_directory_and_the_fixture_is_untouched(tmp_path, path, monkeypatch):
    monkeypatch.setattr(orch_module, "lookup_match", lambda *a, **k: ("found", FOUND, URL))
    case = load_case(path)
    before = digests(case.kb)
    probe = prepare(case, tmp_path, now(), config())
    report = run(probe, config(), launcher_factory=lambda cfg, kb_root: FakeLauncher(named("metformin")))

    assert succeeded(report), outcome(report)
    assert outcome(report)["matches"] == {"metformin": "found"}
    assert digests(case.kb) == before, "the probe wrote into the fixture it was pointed at"
    records = read_records(probe.kb_root)
    [claim] = [r for r in records.values() if isinstance(r, Claim) and r.source_refs == [URL]]
    assert claim.verification_method == "code" and claim.provenance_status == "verified"
    assert [r for r in records.values() if isinstance(r, Relation) and r.target_id == claim.id]
    assert json.loads((probe.run_dir / "probe.json").read_text())["drug_lookup_cache"] == str(probe.cache)


def test_the_cache_is_the_probes_own_not_the_repositorys(tmp_path, monkeypatch):
    """The point of the probe's own cache: a name the repository already cached must not answer for it."""
    seen = {}
    monkeypatch.setattr(
        orch_module, "lookup_match", lambda name, **kwargs: seen.update(kwargs) or ("found", FOUND, URL)
    )
    probe = prepare(load_case(CASES[0]), tmp_path, now(), config())
    run(probe, config(), launcher_factory=lambda cfg, kb_root: FakeLauncher(named("metformin")))
    assert seen["cache_path"] == probe.cache
    assert tmp_path in probe.cache.parents


def test_a_candidate_with_no_drug_name_is_an_honest_failure(tmp_path, monkeypatch):
    """The prompt asks for `drug_name` only when the agent is sure, so an empty field is an outcome, not a bug —
    but it is not a pass either: the probe exists to observe the lookup happen."""
    monkeypatch.setattr(orch_module, "lookup_match", lambda *a, **k: pytest.fail("looked up an unnamed candidate"))
    probe = prepare(load_case(CASES[0]), tmp_path, now(), config())
    report = run(probe, config(), launcher_factory=lambda cfg, kb_root: FakeLauncher(named(None)))
    assert outcome(report)["candidates"] == 1 and outcome(report)["drug_names"] == []
    assert not succeeded(report)


def test_a_failed_lookup_is_reported_and_is_not_a_pass(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise OSError("network down")

    monkeypatch.setattr(orch_module, "lookup_match", boom)
    probe = prepare(load_case(CASES[0]), tmp_path, now(), config())
    report = run(probe, config(), launcher_factory=lambda cfg, kb_root: FakeLauncher(named("metformin")))
    assert outcome(report)["errors"] == {"metformin": "OSError: network down"}
    assert not succeeded(report)
    assert (probe.run_dir / REPORT).is_file()


@pytest.mark.skipif(not LIVE, reason="paid, and queries MyChem.info: set ALS_LIVE_INTERVENE_DRUG_LOOKUP=1")
@pytest.mark.parametrize("path", CASES, ids=lambda p: p.stem)
def test_a_live_agent_names_a_compound_and_the_service_answers(tmp_path, path):
    """A real `intervene` agent, its real prompt, and a real MyChem.info query for whatever it names.

    Asserted structurally only: that a name was given, that the service answered, and that the answer was
    committed with a source URL pointing at the service — never a specific drug property or identifier, which
    would make this test an assertion about pharmacology rather than about the system.
    """
    case = load_case(path)
    probe = prepare(case, tmp_path, now(), config())
    report = run(probe, config())
    seen = outcome(report)
    print(json.dumps({"case": case.name, **seen, "results": str(probe.run_dir)}, indent=2))
    assert seen["drug_names"], "the live agent named no compound: nothing to look up"
    assert not seen["errors"], seen["errors"]
    assert set(seen["matches"].values()) <= {"found", "none", "ambiguous"}
    claims = [r for r in read_records(probe.kb_root).values() if isinstance(r, Claim) and r.agent_id == "drug-lookup"]
    assert claims and all("mychem.info" in ref for c in claims for ref in c.source_refs)
