"""The evaluation scorecard (`evaluation/scorecard.py`): one row per dimension, and `no scorer` where none exists."""

from __future__ import annotations

import json
from pathlib import Path

from kb_factories import claim, decision, experiment, hypothesis, relation

from evaluation import scorecard
from knowledge_base.store import FILES, read_records

DIMENSIONS = [f"EVAL-S{i}" for i in range(1, 9)] + [f"EVAL-AI{i}" for i in range(1, 11)]


def write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))


def kb(tmp_path: Path, *, extra_records: tuple = ()) -> Path:
    root = tmp_path / "kb"
    root.mkdir(parents=True, exist_ok=True)
    records = [
        claim(id="C-1"),
        hypothesis(id="H-1"),
        experiment(
            id="EXP-1",
            hypothesis_id="H-1",
            tested_claims=["C-1"],
            cost="low",
            feasibility="high",
            kind="in_silico",
            dataset_refs=["michaelis-table1-epitopes"],
            dataset_location="local",
        ),
        experiment(id="EXP-2", hypothesis_id="H-1", tested_claims=["C-1"], cost="high", feasibility="low"),
        decision(id="D-1", hypothesis_id="H-1", decision="rejected", experiment_ids=[], value_of_information="low"),
        *extra_records,
    ]
    for record in records:
        with (root / FILES[type(record)]).open("a", encoding="utf-8") as handle:
            handle.write(record.model_dump_json() + "\n")
    return root


def archive(tmp_path: Path, *, audit: bool = False) -> Path:
    results = tmp_path / "results"
    write(
        results / "state.json",
        {
            "run_id": "RUN-1",
            "config_hash": "abc",
            "completed": ["data", "compute", "literature"],
            "phases": {"literature": {"launch": {"stage": "03-literature", "wall_s": 12.5}}},
        },
    )
    write(results / "02-compute-report.json", {"analyses": {"a1": {"result_digest": "d" * 64}, "a2": {}}})
    write(results / "03-literature-result.json", {"cost_usd": 1.25, "agent_count": 2, "status": "completed"})
    write(results / "03-literature-report.json", {"written": ["C-1"], "converted_elements": {"x": ["one"]}})
    # The design phase refused two proposals: one for not naming a contradicting outcome, one for a dataset.
    write(
        results / "07-design-report.json",
        {
            "written": ["EXP-1", "EXP-2"],
            "dropped_elements": {
                "designer@H-1": [
                    "experiment 'x': Value error, experiment EXP-9 has no outcome that would contradict a tested claim: "
                    "not falsifying",
                    "experiment 'y': Value error, experiment EXP-10 names datasets without saying whether they are local "
                    "or remote",
                ]
            },
        },
    )
    write(
        results / "final-proposal.json",
        {
            "run_id": "RUN-1",
            "proposal": {"outcome": "proposal", "failure_modes": [1, 2], "mechanism": {"cites": ["C-1"]}},
            "gate": {
                "passed": True,
                "unknown_ids": {},
                "cites_contradicted_or_contested": {},
                "computed_claims_cited": [],
            },
        },
    )
    (results / "03-literature-agent-tools.jsonl").write_text(
        json.dumps(
            {
                "label": "literature-1",
                "tool": "mcp__kb__kb_search",
                "status": "ok",
                "phase": "literature",
                "input_preview": "{}",
                "result_preview": "{}",
                "result_bytes": 2,
            }
        )
        + "\n"
    )
    if audit:
        write(
            results / "audit" / "citations.json",
            {
                "ladder": {
                    "unit": "cites tags",
                    "total": 5,
                    "by_rung": {
                        "quote_checked": 1,
                        "source_text_absent": 1,
                        "no_source_ref": 1,
                        "no_quote": 1,
                        "derived": 1,
                    },
                },
                "weaknesses": [{"id": "C-2", "kind": "quote cannot be checked", "detail": "text_not_available"}],
            },
        )
    return results


def card(tmp_path: Path, *, extra_records: tuple = (), **kw) -> dict:
    return scorecard.scorecard(archive(tmp_path, **kw), kb(tmp_path, extra_records=extra_records))


def test_there_is_one_row_per_evaluation_dimension(tmp_path):
    data = card(tmp_path)

    assert [row["id"] for row in data["rows"]] == sorted(
        DIMENSIONS, key=lambda i: (i.split("-")[1][:2], int("".join(c for c in i if c.isdigit())))
    )
    assert sum(data["counts"].values()) == len(DIMENSIONS) == 18


def test_the_rows_with_no_scorer_are_named_and_each_carries_its_reason(tmp_path):
    """The temptation is to quietly fill one in with something plausible; this test is the guard."""
    data = card(tmp_path)

    none = {row["id"]: row for row in data["rows"] if row["status"] == "no scorer"}
    assert set(none) == {"EVAL-S2", "EVAL-S3", "EVAL-S5"}
    assert all(row["limit"] and row["value"] is None for row in none.values())
    assert "domain expert" in none["EVAL-S3"]["limit"]
    assert "`no scorer`" in scorecard.render(data)


def test_falsifiability_and_tractability_are_read_from_the_experiment_records(tmp_path):
    data = {row["id"]: row for row in card(tmp_path)["rows"]}

    assert data["EVAL-S6"]["value"] == {
        "experiments_written": 2,
        "proposals_refused_as_not_falsifying": 1,
        "proposals_refused_for_other_reasons": 1,
    }
    assert "by construction" in data["EVAL-S6"]["limit"]
    assert data["EVAL-S8"]["value"]["kind"] == {"in_silico": 1, "wet_lab": 1}
    assert data["EVAL-S8"]["value"]["cost"] == {"low": 1, "high": 1}


def test_cost_and_latency_come_from_the_archive_not_from_a_guess(tmp_path):
    data = {row["id"]: row for row in card(tmp_path)["rows"]}

    assert data["EVAL-AI9"]["value"]["total_usd"] == 1.25
    assert data["EVAL-AI9"]["value"]["usd_by_stage"] == {"03-literature": 1.25}
    assert data["EVAL-AI10"]["value"] == {"wall_s_by_phase": {"literature": 12.5}, "total_wall_s": 12.5}


def test_without_the_audit_the_literature_row_is_null_with_a_reason_not_zero(tmp_path):
    data = {row["id"]: row for row in card(tmp_path)["rows"]}

    assert data["EVAL-S4"]["status"] == "null" and data["EVAL-S4"]["value"] is None
    assert "citation_audit" in data["EVAL-S4"]["limit"]
    assert data["EVAL-AI1"]["value"]["quotes_of_cited_claims"] is None  # the surface is thinner, and says so


def test_with_the_audit_the_support_ladder_reaches_the_scorecard(tmp_path):
    data = {row["id"]: row for row in card(tmp_path, audit=True)["rows"]}

    assert data["EVAL-S4"]["status"] == "scored"
    assert data["EVAL-S4"]["value"] == {
        "claim_citation_tags": 4,
        "with_a_checked_quote": 1,
        "quote_whose_source_text_is_absent": 1,
        "quote_with_no_source_ref": 1,
        "no_quote": 1,
    }
    assert data["EVAL-AI7"]["value"]["weaknesses"] == {"known_weaknesses": 1, "stated_by_the_proposal": None}


def test_citation_accuracy_waits_for_a_human_and_then_reports_its_denominator(tmp_path):
    results, root = archive(tmp_path, audit=True), kb(tmp_path)
    empty = {row["id"]: row for row in scorecard.scorecard(results, root)["rows"]}
    assert empty["EVAL-AI2"]["status"] == "null" and "has not been read" in empty["EVAL-AI2"]["limit"]

    filled = tmp_path / "citations.md"
    filled.write_text(
        "| id | rung | claim | quote | sentence | source | verdict | note |\n"
        "|---|---|---|---|---|---|---|---|\n"
        "| C-1 | quote_checked | x | y | s | z | supports |  |\n"
        "| C-2 | quote_checked | x | y | s | z | cited to limit |  |\n"
    )

    data = {row["id"]: row for row in scorecard.scorecard(results, root, filled)["rows"]}

    assert data["EVAL-AI2"]["status"] == "scored"
    assert data["EVAL-AI2"]["value"]["accuracy"] == 1.0 and data["EVAL-AI2"]["value"]["denominator"] == 1
    assert "the reader is the system's author" in data["EVAL-AI2"]["limit"]


def test_the_self_detection_row_says_whose_errors_it_counts(tmp_path):
    """Our validation refusing agent content is us catching them, which the dimension's name would hide."""
    row = next(r for r in card(tmp_path)["rows"] if r["id"] == "EVAL-AI7")

    assert row["value"]["elements_converted_instead_of_dropped"] == {"literature": 1}
    assert "**us** catching the agents" in row["limit"]


def test_there_is_no_overall_score(tmp_path):
    data = card(tmp_path)

    assert "score" not in data and "total" not in data
    assert "no overall score" in scorecard.render(data)


# --- EVAL-AI4, hypothesis diversity by causal-edge overlap ---------------------------------------------------


def causal_claim(cid: str, hid: str, lid: str, cause: str, effect: str) -> tuple:
    """A claim with a causal edge, `part_of` one hypothesis — the shape `hypothesis_diversity` reads."""
    return (
        claim(
            id=cid,
            content=f"{cause} causes {effect}.",
            cause_ref=cause,
            effect_ref=effect,
            context="ALS/FTD",
            mechanism="m",
        ),
        relation(id=lid, type="part_of", source_id=cid, target_id=hid),
    )


def test_fewer_than_two_hypotheses_is_null_not_a_fabricated_number(tmp_path):
    """The base fixture has exactly one hypothesis, H-1, and no `part_of` claim for it."""
    row = next(r for r in card(tmp_path)["rows"] if r["id"] == "EVAL-AI4")

    assert row["status"] == "null" and row["value"] is None
    assert "fewer than two" in row["limit"]


def test_two_hypotheses_with_disjoint_causal_edges_are_fully_diverse(tmp_path):
    extra = (
        *causal_claim("C-10", "H-1", "L-10", "N-1", "N-2"),
        hypothesis(id="H-2"),
        *causal_claim("C-11", "H-2", "L-11", "N-3", "N-4"),
    )
    row = next(r for r in card(tmp_path, extra_records=extra)["rows"] if r["id"] == "EVAL-AI4")

    assert row["status"] == "scored"
    assert row["value"] == {
        "hypotheses": 2,
        "pairs_compared": 1,
        "with_a_causal_claim_of_their_own": 2,
        "diversity": 1.0,
    }


def test_two_hypotheses_with_the_same_causal_edge_are_not_diverse(tmp_path):
    extra = (
        *causal_claim("C-10", "H-1", "L-10", "N-1", "N-2"),
        hypothesis(id="H-2"),
        *causal_claim("C-11", "H-2", "L-11", "N-1", "N-2"),
    )
    row = next(r for r in card(tmp_path, extra_records=extra)["rows"] if r["id"] == "EVAL-AI4")

    assert row["value"]["diversity"] == 0.0


def test_the_diversity_value_matches_a_hand_computed_jaccard(tmp_path):
    """Two hypotheses sharing one edge of three: Jaccard similarity 1/3, diversity 2/3 — not just a type check."""
    extra = (
        *causal_claim("C-10", "H-1", "L-10", "N-1", "N-2"),
        *causal_claim("C-11", "H-1", "L-11", "N-3", "N-4"),
        hypothesis(id="H-2"),
        *causal_claim("C-12", "H-2", "L-12", "N-1", "N-2"),
        *causal_claim("C-13", "H-2", "L-13", "N-5", "N-6"),
    )
    row = next(r for r in card(tmp_path, extra_records=extra)["rows"] if r["id"] == "EVAL-AI4")

    assert row["value"]["diversity"] == round(1 - 1 / 3, 4)


def test_three_hypotheses_average_over_every_pair(tmp_path):
    """H-1 and H-3 assert the same edge, H-2 a disjoint one: similarities 0, 1, 0 over three pairs, mean 1/3."""
    extra = (
        *causal_claim("C-10", "H-1", "L-10", "N-1", "N-2"),
        hypothesis(id="H-2"),
        *causal_claim("C-11", "H-2", "L-11", "N-3", "N-4"),
        hypothesis(id="H-3"),
        *causal_claim("C-12", "H-3", "L-12", "N-1", "N-2"),
    )
    row = next(r for r in card(tmp_path, extra_records=extra)["rows"] if r["id"] == "EVAL-AI4")

    assert row["value"] == {
        "hypotheses": 3,
        "pairs_compared": 3,
        "with_a_causal_claim_of_their_own": 3,
        "diversity": round(1 - 1 / 3, 4),
    }


def test_hypotheses_with_no_causal_claim_count_as_fully_similar_by_convention(tmp_path):
    extra = (
        hypothesis(id="H-2"),
        claim(id="C-20", content="a non-causal note"),
        relation(id="L-20", type="part_of", source_id="C-20", target_id="H-2"),
    )
    row = next(r for r in card(tmp_path, extra_records=extra)["rows"] if r["id"] == "EVAL-AI4")

    assert row["value"] == {
        "hypotheses": 2,
        "pairs_compared": 1,
        "with_a_causal_claim_of_their_own": 0,
        "diversity": 0.0,
    }
    assert "by convention" in row["limit"]


def test_a_superseded_hypothesis_is_excluded_from_the_comparison(tmp_path):
    extra = (
        *causal_claim("C-10", "H-1", "L-10", "N-1", "N-2"),
        hypothesis(id="H-2"),
        *causal_claim("C-11", "H-2", "L-11", "N-9", "N-8"),
        hypothesis(id="H-3"),
        relation(id="L-99", type="supersedes", source_id="H-3", target_id="H-2"),
    )
    row = next(r for r in card(tmp_path, extra_records=extra)["rows"] if r["id"] == "EVAL-AI4")

    # Had H-2 (superseded) been counted, there would be 3 hypotheses and 3 pairs, not 2 and 1.
    assert row["value"]["hypotheses"] == 2 and row["value"]["pairs_compared"] == 1
    assert row["value"]["diversity"] == 1.0  # H-1's edge vs H-3's empty set: no overlap


def test_a_real_archived_run_reproduces_its_known_diversity_value():
    """Frozen output of a real run's knowledge base (`tests/fixtures/scorecard_diversity/`), not a
    synthetic case: a regression check that the function still scores real pipeline output as it did when it
    was first run live, alongside the hand-built cases above that cover its individual rules."""
    fixture = Path(__file__).parent / "fixtures" / "scorecard_diversity"
    row = scorecard.hypothesis_diversity(read_records(fixture), "pl-prod-005")

    assert row["value"] == {
        "hypotheses": 3,
        "pairs_compared": 3,
        "with_a_causal_claim_of_their_own": 3,
        "diversity": 0.9583,
    }


def test_the_scored_diversity_row_reaches_the_rendered_scorecard(tmp_path):
    extra = (
        *causal_claim("C-10", "H-1", "L-10", "N-1", "N-2"),
        hypothesis(id="H-2"),
        *causal_claim("C-11", "H-2", "L-11", "N-3", "N-4"),
    )
    text = scorecard.render(card(tmp_path, extra_records=extra))

    assert "| EVAL-AI4 | Hypothesis diversity |" in text and "no scorer" not in text.split("EVAL-AI4")[1][:200]


def _novelty_report(results: Path, verdicts: dict[str, str | None], provenance: bool = True) -> None:
    def block(hid: str) -> dict:
        # H-1 and H-2 both cite `W-shared` and both agents declared its paper, so the per-entry counts sum to
        # two readings of one work: the case the distinct count exists for.
        shared = hid in ("H-1", "H-2")
        kept = [
            {"openalex_id": "W-shared", "evidence_depth": "full_text" if shared else "abstract"},
            {"openalex_id": f"W-own-{hid}", "evidence_depth": "abstract"},
        ]
        return {
            "provenance": {
                "kept": kept,
                "dropped": [],
                "rationale_only": [],
                "works_listed": 2,
                "works_dropped": 1,
                "works_read_in_full": 1 if shared else 0,
            }
        }

    entries = [
        (
            {
                "hypothesis_id": hid,
                "status": "assessed",
                "novelty": {"verdict": v, "full_texts_read": ["10.1/a"] if hid in ("H-1", "H-2") else []},
            }
            if v
            else {"hypothesis_id": hid, "status": "unverified", "error": "x"}
        )
        | (block(hid) if provenance else {})
        for hid, v in verdicts.items()
    ]
    write(
        results / "13-novelty-report.json",
        {"status": "assessed", "entries": entries, "sources_searched": ["openalex"], "searched_at": "2026-10-11"},
    )


def test_novelty_is_unmeasured_not_zero_before_the_stage_has_run(tmp_path):
    rows = {r["id"]: r for r in card(tmp_path)["rows"]}

    for rid in ("EVAL-S7", "EVAL-AI3"):
        assert rows[rid]["status"] == "null" and rows[rid]["value"] is None
        assert "pipeline.cli novelty" in rows[rid]["limit"]


def test_novelty_counts_verdicts_and_says_what_it_does_not_show(tmp_path):
    results = archive(tmp_path)
    _novelty_report(
        results, {"H-1": "anticipated", "H-2": "no_prior_art_found", "H-3": "no_prior_art_found", "H-4": None}
    )
    rows = {r["id"]: r for r in scorecard.hypothesis_novelty(results)}

    assert set(rows) == {"EVAL-S7", "EVAL-AI3"}
    value = rows["EVAL-S7"]["value"]
    assert (value["hypotheses"], value["assessed"], value["unassessed"]) == (4, 3, 1)
    assert (value["anticipated"], value["partly_anticipated"], value["no_prior_art_found"]) == (1, 0, 2)
    # Two of the four agents declared the same paper, and this figure sums their declared lists, so it reads
    # two: it counts declarations, never distinct papers.
    assert value["sources_searched"] == ["openalex"] and value["full_texts_read"] == 2
    assert rows["EVAL-S7"]["value"] == rows["EVAL-AI3"]["value"] and rows["EVAL-S7"]["status"] == "scored"
    assert "not a measure of novelty" in rows["EVAL-AI3"]["limit"]


def test_novelty_counts_the_works_the_host_kept_dropped_and_read_in_full(tmp_path):
    """An `assessed` entry no longer means every work the agent named survived, so the row says how many did."""
    results = archive(tmp_path)
    _novelty_report(results, {"H-1": "anticipated", "H-2": "partly_anticipated", "H-3": None})
    value = {r["id"]: r for r in scorecard.hypothesis_novelty(results)}["EVAL-S7"]

    assert (value["value"]["works_listed"], value["value"]["works_read_in_full"]) == (4, 2)
    # H-1 and H-2 both read the one shared work, so the sum says two readings and the union says one work.
    assert value["value"]["distinct_works_read_in_full"] == 1
    # A drop is a fact about provenance, so the refused entry's drop counts too.
    assert value["value"]["works_dropped"] == 3
    assert "works_dropped" in value["limit"] and "does not mean every work the agent named survived" in value["limit"]


def test_the_work_counts_are_absent_not_zero_for_a_report_without_the_block(tmp_path):
    results = archive(tmp_path)
    _novelty_report(results, {"H-1": "anticipated"}, provenance=False)
    value = {r["id"]: r for r in scorecard.hypothesis_novelty(results)}["EVAL-S7"]["value"]

    assert value["works_listed"] is None and value["works_dropped"] is None and value["assessed"] == 1
    assert value["works_read_in_full"] is None and value["distinct_works_read_in_full"] is None


def test_the_scored_novelty_rows_reach_the_rendered_scorecard(tmp_path):
    results = archive(tmp_path)
    _novelty_report(results, {"H-1": "partly_anticipated"})
    text = scorecard.render(scorecard.scorecard(results, kb(tmp_path)))

    assert "| EVAL-S7 | Novelty |" in text and "partly_anticipated" in text
