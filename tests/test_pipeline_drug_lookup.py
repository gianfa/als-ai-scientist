"""The host-side drug lookup after `intervene`: off by default, one claim per name, honest wording per outcome."""

from __future__ import annotations

import json

import pytest
from test_pipeline_orchestrator import ALL, intervene, orchestrator

from knowledge_base.schemas import Claim, Intervention, Relation
from knowledge_base.store import read_records
from pipeline import orchestrator as orch_module
from pipeline.launcher import FakeLauncher
from tools.data.pharmacology import DrugRecord

PHASES = ("literature", "generate", "review", "verify", "design", "arbitrate", "intervene")
URL = "https://mychem.info/v1/query?q=fake"
FOUND = DrugRecord(
    query="x",
    drugbank_id="DB00331",
    chembl_id="CHEMBL1431",
    chebi_id=None,
    pubchem_cid=None,
    unii=None,
    tradename="metformin",
    retrieved_at="2026-10-10T00:00:00+00:00",
    source_url=URL,
)


def named(*names):
    """An `intervene` fake whose candidates carry the given drug names (None: the agent gave none)."""

    def launch(args):
        out = intervene(args)
        base = out["outputs"][0]["output"]["candidates"][0]
        out["outputs"][0]["output"]["candidates"] = [
            base | {"name": f"cand-{i}", "drug_name": n} for i, n in enumerate(names)
        ]
        return out

    return ALL | {"intervene": launch}


def enabled(o):
    intervene_cfg = o.config.phases.intervene.model_copy(update={"drug_lookup_enabled": True})
    o.config = o.config.model_copy(update={"phases": o.config.phases.model_copy(update={"intervene": intervene_cfg})})
    return o


def lookup_claims(tmp_path):
    records = read_records(tmp_path / "kb")
    return records, [r for r in records.values() if isinstance(r, Claim) and r.source_refs == [URL]]


def test_the_lookup_is_off_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr(orch_module, "lookup_match", lambda *a, **k: pytest.fail("looked up while disabled"))
    state = orchestrator(tmp_path, FakeLauncher(named("metformin"))).run(PHASES)
    assert "drug_lookup" not in state["phases"]["intervene"]
    assert lookup_claims(tmp_path)[1] == []


@pytest.mark.parametrize(
    "match, record, words",
    [("found", FOUND, "exactly one record"), ("none", None, "no record"), ("ambiguous", None, "several records")],
)
def test_each_outcome_commits_its_own_wording_and_a_depends_on(tmp_path, monkeypatch, match, record, words):
    monkeypatch.setattr(orch_module, "lookup_match", lambda *a, **k: (match, record, URL))
    enabled(orchestrator(tmp_path, FakeLauncher(named("metformin")))).run(PHASES)
    records, [claim] = lookup_claims(tmp_path)
    assert words in claim.content and claim.verification_method == "code" and claim.provenance_status == "verified"
    [cand] = [r for r in records.values() if isinstance(r, Intervention)]
    [rel] = [r for r in records.values() if isinstance(r, Relation) and r.target_id == claim.id]
    assert (rel.type, rel.source_id) == ("depends_on", cand.id)
    report = json.loads((tmp_path / "run" / "07-intervene-report.json").read_text())
    assert report["drug_lookup"]["metformin"] == {"match": match, "claim_id": claim.id, "interventions": [cand.id]}


def test_a_claim_the_store_already_held_is_reported_under_the_id_it_kept(tmp_path, monkeypatch):
    """The report must name the ID the store kept, not the one the lookup proposed (a report naming the dropped ID is a defect)."""
    monkeypatch.setattr(orch_module, "lookup_match", lambda *a, **k: ("found", FOUND, URL))
    enabled(orchestrator(tmp_path, FakeLauncher(named("metformin")))).run(PHASES)
    first = json.loads((tmp_path / "run" / "07-intervene-report.json").read_text())
    kept = first["drug_lookup"]["metformin"]["claim_id"]

    # `intervene` again, same run, fresh results directory: the lookup proposes the same fact, which the store
    # already holds, so it comes back as `seen_again` under the ID it was first given. (A *second* run over the
    # same store cannot be used here: its own hypothesis deduplicates into the first run's, so it has no
    # survivor of its own and `intervene` is skipped.)
    enabled(orchestrator(tmp_path, FakeLauncher(named("metformin")), run_dir="again")).run(("intervene",))
    again = json.loads((tmp_path / "again" / "01-intervene-report.json").read_text())
    assert kept in again["seen_again"].values(), "the store did not deduplicate: nothing is being tested"
    assert again["drug_lookup"]["metformin"]["claim_id"] == kept
    assert kept not in again["written"]
    assert len(lookup_claims(tmp_path)[1]) == 1  # the fact was stored once, not twice


def test_two_candidates_with_one_name_cost_one_lookup_and_two_relations(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(orch_module, "lookup_match", lambda name, **k: calls.append(name) or ("found", FOUND, URL))
    enabled(orchestrator(tmp_path, FakeLauncher(named("metformin", "Metformin")))).run(PHASES)
    records, [claim] = lookup_claims(tmp_path)
    assert len(calls) == 1
    assert len([r for r in records.values() if isinstance(r, Relation) and r.target_id == claim.id]) == 2


def test_a_failed_lookup_commits_nothing_and_is_reported(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise OSError("network down")

    monkeypatch.setattr(orch_module, "lookup_match", boom)
    state = enabled(orchestrator(tmp_path, FakeLauncher(named("metformin")))).run(PHASES)
    assert lookup_claims(tmp_path)[1] == []
    report = (tmp_path / "run" / "07-intervene-report.json").read_text()
    assert "network down" in report and "OSError" in report
    assert state["completed"][-1] == "intervene"
