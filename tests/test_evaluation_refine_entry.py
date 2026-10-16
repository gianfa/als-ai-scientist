"""Replay of the `refine` entry condition (`evaluation/refine_entry.py`): pure, over records, no network."""

from __future__ import annotations

from datetime import timedelta

from kb_factories import NOW, claim, hypothesis, relation

from evaluation import refine_entry
from knowledge_base.schemas import Event

LATER = NOW + timedelta(hours=1)


def event(**overrides):
    fields = {
        "id": "V-1",
        "run_id": "RUN-1",
        "created_at": NOW,
        "event": "written",
        "record_id": "L-1",
        "agent_id": "verifier-1",
        "detail": None,
    }
    fields.update(overrides)
    return Event(**fields)


def kb(*records):
    return {r.id: r for r in records}


def anomaly(target="C-1", source="C-2", rid="L-2", **kw):
    """A verified contradiction: the only thing that makes a claim `contradicted`."""
    return relation(id=rid, type="contradicts", source_id=source, target_id=target, **kw)


def hypothesis_with_claims(*claim_ids, hid="H-1", run_id="RUN-1"):
    records = [hypothesis(id=hid, run_id=run_id)]
    records += [
        relation(
            id=f"L-{90 + i}",
            type="part_of",
            source_id=cid,
            target_id=hid,
            justification_method="agent_inference",
            provenance_status="unverified",
            source_refs=[],
        )
        for i, cid in enumerate(claim_ids)
    ]
    return records


def test_a_hypothesis_with_a_verified_contradiction_enters():
    records = kb(
        claim(id="C-1"),
        claim(id="C-2", content="Contradicting evidence."),
        *hypothesis_with_claims("C-1"),
        anomaly(),
        event(),
    )

    data = refine_entry.report(records)

    assert data["totals"]["entering_as_of_verify"] == 1
    assert data["totals"]["entering_today"] == 1
    row = data["runs"][0]
    assert row["as_of_verify"] == [{"hypothesis": "H-1", "claims": 1, "anomalous": {"C-1": "contradicted"}}]


def test_an_unverified_objection_does_not_admit_the_hypothesis():
    """The whole point of the condition: an agent's assertion is not an anomaly until `verify` confirms it."""
    records = kb(
        claim(id="C-1"),
        claim(
            id="C-2",
            content="An agent's objection.",
            origin="agent",
            provenance_status="unverified",
            verification_method="none",
            source_refs=[],
        ),
        *hypothesis_with_claims("C-1"),
        anomaly(justification_method="agent_inference", provenance_status="unverified"),
        event(),
    )

    data = refine_entry.report(records)

    assert data["totals"]["entering_as_of_verify"] == 0
    assert data["runs"][0]["today"] == []


def test_a_contradiction_written_after_verify_counts_today_but_not_as_of_verify():
    """The knowledge base is shared and grows: a later run can contradict this run's claim, and a phase that
    ran before that could not have known."""
    records = kb(
        claim(id="C-1"),
        claim(id="C-2", content="Contradicting evidence.", created_at=LATER),
        *hypothesis_with_claims("C-1"),
        anomaly(created_at=LATER),
        event(),
    )

    row = refine_entry.report(records)["runs"][0]

    assert row["as_of_verify"] == []
    assert [item["hypothesis"] for item in row["today"]] == ["H-1"]


def test_a_run_whose_verify_wrote_nothing_is_null_and_not_zero():
    records = kb(
        claim(id="C-1"),
        claim(id="C-2", content="Contradicting evidence."),
        *hypothesis_with_claims("C-1"),
        anomaly(),
        event(agent_id="reviewer-1"),
    )

    data = refine_entry.report(records)

    assert data["runs"][0]["as_of_verify"] is None
    assert data["totals"]["runs_without_a_verify_record"] == 1
    assert data["totals"]["entering_as_of_verify"] == 0  # the run is excluded, not counted as zero
    assert "no verify record" in refine_entry.render(data)


def test_what_verify_confirmed_is_reported_by_relation_type():
    records = kb(
        claim(id="C-1"),
        claim(id="C-2", content="Contradicting evidence."),
        claim(id="C-3", content="Supporting evidence."),
        *hypothesis_with_claims("C-1"),
        anomaly(rationale="confirmed by verifier-1 (verifies L-9): the quote is in the source"),
        relation(
            id="L-3",
            type="supports",
            source_id="C-3",
            target_id="C-1",
            rationale="confirmed by verifier-1 (verifies L-8): the quote is in the source",
        ),
        event(),
    )

    row = refine_entry.report(records)["runs"][0]

    assert row["verify_confirmed"] == {"contradicts": 1, "supports": 1}
    assert [item["anomalous"]["C-1"] for item in row["today"]] == ["contested"]


def test_a_superseded_hypothesis_is_not_offered_for_revision_again():
    records = kb(
        claim(id="C-1"),
        claim(id="C-2", content="Contradicting evidence."),
        *hypothesis_with_claims("C-1"),
        anomaly(),
        event(),
        hypothesis(id="H-2"),
        relation(
            id="L-4",
            type="supersedes",
            source_id="H-2",
            target_id="H-1",
            justification_method="agent_inference",
            provenance_status="unverified",
            source_refs=[],
        ),
    )

    data = refine_entry.report(records)

    assert [item["hypothesis"] for item in data["runs"][0]["today"]] == []


def test_the_cli_prints_both_formats(tmp_path, capsys, monkeypatch):
    records = kb(
        claim(id="C-1"),
        claim(id="C-2", content="Contradicting evidence."),
        *hypothesis_with_claims("C-1"),
        anomaly(),
        event(),
    )
    monkeypatch.setattr(refine_entry, "read_records", lambda root: records)

    assert refine_entry.main(["--kb", str(tmp_path), "--format", "markdown"]) == 0
    assert "would enter (as of verify)" in capsys.readouterr().out
    assert refine_entry.main(["--kb", str(tmp_path)]) == 0
    assert '"entering_as_of_verify": 1' in capsys.readouterr().out
