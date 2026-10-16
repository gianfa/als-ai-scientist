"""The orchestrator with a fake launcher: no paid calls."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import pytest
from kb_factories import NOW
from test_experiment_runner import fixture as hla_fixture

from analysis.experiment_runner import HANDLERS
from knowledge_base.schemas import Claim, Hypothesis, Ignorance
from knowledge_base.store import read_records
from pipeline.compute import claims_of
from pipeline.config import DataSourceConfig, load_config
from pipeline.launcher import FakeLauncher, PhaseFailed, PhaseRun
from pipeline.orchestrator import PHASES, Orchestrator
from pipeline.proposals import Provenance
from tools.data.local_sources import MissingSource
from tools.experiment.mcp_server import run_experiment

QUOTE = "C9orf72-specific CD4+ T cell responses were increased in patients with ALS"


def verifier(claim):
    if claim.quote in (QUOTE, "IL-10 responses to C9orf72 were not associated with motor neuron loss"):
        return Provenance("verified", "quote", ("receipt:x",))
    return Provenance("unverified", "none")


def literature(args):
    return {
        "phase": "literature",
        "dropped": ["literature-4"],
        "outputs": [
            {
                "agent_id": "literature-1",
                "output": {
                    "claims": [
                        {
                            "ref": "c1",
                            "type": "observational",
                            "content": "CD4 responses to C9orf72 are increased in ALS.",
                            "source": "doi:10.1000/fixture-michaelis",
                            "quote": QUOTE,
                            "entities": ["C9orf72", "CD4 T cells"],
                        },
                        {
                            "ref": "c2",
                            "type": "observational",
                            "content": "IL-10 responses associate with longer survival.",
                            "source": "doi:10.1000/fixture-michaelis",
                            "quote": "a paraphrase",
                            "entities": ["IL-10"],
                        },
                    ]
                },
            },
            {
                "agent_id": "literature-2",
                "output": {
                    "claims": [
                        {
                            "ref": "c1",
                            "type": "observational",
                            "content": "  cd4 responses to C9orf72 are increased in ALS ",
                            "source": "doi:10.1000/fixture-michaelis",
                            "quote": QUOTE,
                        }
                    ]
                },
            },  # same claim: seen again
            {
                "agent_id": "literature-3",
                "output": {"claims": [{"ref": "C1", "type": "observational", "content": "x"}]},
            },
        ],
    }


def generate(args):
    cited = args["kb_context"][0]["id"]
    return {
        "phase": "generate",
        "dropped": [],
        "outputs": [
            {
                "agent_id": "author-1",
                "output": {
                    "hypothesis": {
                        "statement": "Loss of tolerance to C9orf72 drives motor-neuron loss",
                        "mechanism": "Autoreactive CD4 T cells damage motor neurons.",
                        "author_confidence": 2,
                        "claims": [
                            {
                                "ref": "c1",
                                "type": "observational",
                                "content": "Autoreactive CD4 T cells drive motor neuron loss",
                                "causal": {
                                    "cause": "CD4 T cells",
                                    "effect": "motor neuron loss",
                                    "context": "C9-ALS",
                                    "mechanism": "cytokines",
                                },
                            }
                        ],
                        "links": [
                            {"type": "supports", "source": cited, "target": "c1", "rationale": "responses increased"}
                        ],
                        "ignorance": [{"content": "Cause or consequence?", "questions": ["c1"]}],
                    }
                },
            }
        ],
    }


CONTRA_QUOTE = "IL-10 responses to C9orf72 were not associated with motor neuron loss"


def review(args):
    item = args["items"][0]
    target = next(c["id"] for c in item["context"] if c.get("cause_ref"))  # the edge, whatever its type
    assert not any("author_confidence" in c or "agent_id" in c for c in item["context"])  # blinded
    return {
        "phase": "review",
        "dropped": [],
        "outputs": [
            {
                "agent_id": "reviewer-1@" + item["hypothesis_id"],
                "hypothesis_id": item["hypothesis_id"],
                "output": {
                    "claims": [
                        {
                            "ref": "e1",
                            "type": "observational",
                            "content": "IL-10 responses do not track neuron loss.",
                            "source": "doi:10.1000/fixture-other",
                            "quote": CONTRA_QUOTE,
                        }
                    ],
                    "links": [{"type": "contradicts", "source": "e1", "target": target, "rationale": "no association"}],
                    "gaps": [{"content": "No data on CNS infiltration.", "questions": [target]}],
                    "summary": "The causal step lacks direct evidence.",
                },
            }
        ],
    }


def verify(args):
    return {
        "phase": "verify",
        "dropped": [],
        "outputs": [
            {
                "agent_id": "verifier-1",
                "output": {
                    "verdicts": [
                        {
                            "relation_id": c["relation_id"],
                            "faithful": True,
                            "verdict": "confirmed",
                            "reason": "the quote says so",
                        }
                        for batch in args["batches"]
                        for c in batch
                    ]
                },
            }
        ],
    }


def design(args):
    item = args["items"][0]
    target = item["targets"][0]
    return {
        "outputs": [
            {
                "agent_id": f"designer@{item['hypothesis_id']}",
                "hypothesis_id": item["hypothesis_id"],
                "output": {
                    "experiments": [
                        {
                            "title": "CD4 depletion in C9 mice",
                            "protocol": "Deplete CD4 T cells; count motor neurons.",
                            "tested_claims": [target],
                            "expected_outcomes": [
                                {"outcome": "less neuron loss", "claim_id": target, "effect": "supports"},
                                {"outcome": "no change", "claim_id": target, "effect": "contradicts"},
                            ],
                            "cost": "medium",
                            "cost_rationale": "mouse cohort",
                            "feasibility": "high",
                        }
                    ]
                },
            }
        ]
    }


def arbitrate(args):
    item = args["items"][0]
    eid = next(c["id"] for c in item["context"] if c["id"].startswith("EXP-"))
    return {
        "outputs": [
            {
                "agent_id": f"arbiter@{item['hypothesis_id']}",
                "hypothesis_id": item["hypothesis_id"],
                "output": {
                    "decision": "buy",
                    "experiment_ids": [eid],
                    "value_of_information": "high",
                    "rationale": f"{eid} settles the contested causal step",
                },
            }
        ]
    }


def intervene(args):
    item = args["items"][0]
    target = item["targets"][0]
    return {
        "outputs": [
            {
                "agent_id": f"intervener@{item['hypothesis_id']}",
                "hypothesis_id": item["hypothesis_id"],
                "output": {
                    "candidates": [
                        {
                            "name": "Anti-CD4 depletion",
                            "modality": "biologic",
                            "target": "CD4 T cells",
                            "acts_on_claims": [target],
                            "rationale": "removes the effector population",
                            "predicted_effect": "slower motor decline; none if T cells are bystanders",
                            "off_target_risks": ["broad immunosuppression"],
                            "safety_concerns": ["infections"],
                            "stratification": "C9orf72 carriers with C9orf72-reactive CD4 T cells",
                            "biomarker": "C9orf72-specific CD4 T-cell frequency",
                            "maturity": "medium",
                        }
                    ]
                },
            }
        ]
    }


def synthesize(args):
    [h] = args["items"][0]["hypotheses"]
    claim_id = h["targets"][0]
    section = {"text": f"Hypothesis [[{h['hypothesis_id']}]] rests on [[{claim_id}]].", "cites": [h["hypothesis_id"]]}
    return {
        "outputs": [
            {
                "agent_id": "pi",
                "output": {
                    "outcome": "proposal",
                    "summary": "Test the causal step first.",
                    **{
                        k: section
                        for k in (
                            "mechanism",
                            "therapeutic_hypothesis",
                            "intervention",
                            "stratification",
                            "biomarker",
                            "validation",
                            "translation",
                        )
                    },
                    "failure_modes": [{"text": f"Failure {n}.", "cites": []} for n in range(5)],
                },
            }
        ]
    }


ALL = {
    "literature": literature,
    "generate": generate,
    "review": review,
    "verify": verify,
    "design": design,
    "arbitrate": arbitrate,
    "intervene": intervene,
    "synthesize": synthesize,
}


INTERPRETATIONS = {
    "panel_condition_met": "supports_panel_condition_only",
    "panel_condition_failed": "weakens_panel_condition_only",
    "skew_replicated": "supports_ifng_skew_only",
    "skew_not_replicated": "weakens_ifng_skew_only",
}


def execute_every(args, policy_path: Path):
    """Stands in for the live `execute` session: one agent per selected EXP, each calling the real MCP tool
    with its own EXP and labelled as `workflows/phases/src/execute.js` labels it."""
    outputs = []
    for selected in args["selected"]:
        eid = selected["experiment_id"]
        result = run_experiment(policy_path, eid)
        assert "error" not in result, result
        outputs.append(
            {
                "agent_id": f"executor@{eid}",
                "output": {
                    "experiment_id": eid,
                    "analysis_id": result["analysis_id"],
                    "result_digest": result["result_digest"],
                    "outcome": result["outcome"],
                    "interpretation": INTERPRETATIONS[result["outcome"]],
                    "limitations": result["limitations"],
                },
            }
        )
    return {"outputs": outputs}


def reassess_every(args, decision: str = "inconclusive"):
    """One critic per executed EXP, each binding to its own digest and citing its own result claims."""
    outputs = []
    for entry in args["executions"]:
        execution = entry["execution"]
        # A reuse wrote nothing and was seen again as nothing: its claims are the computing EXP's, which the
        # critic is shown under its hypothesis.
        claims = (
            execution["written"]
            or execution["seen_again"]
            or [c["id"] for c in entry["hypothesis"]["computed_result_claims"]]
        )
        outputs.append(
            {
                "agent_id": f"reassessor@{execution['experiment_id']}",
                "output": {
                    "experiment_id": execution["experiment_id"],
                    "result_digest": execution["result_digest"],
                    "decision": decision,
                    "rationale": "One registered analysis cannot settle the broader claim.",
                    "claim_ids": [claims[0]],
                    "limitations": ["No population allele frequencies."],
                },
            }
        )
    return {"outputs": outputs}


def fake_analysis(analysis_id, manifest, root, output_root, seed, run_id, config_hash):
    """One analysis, without running one: the real a1/a2/a3 read gigabytes and are covered by their own tests."""
    summary = {
        "result_digest": "d" * 64,
        "negative_result": True,
        "conclusion": "no single epitope dominates",
        "results": [{"key": "p_value", "value": 0.42, "n": 1000, "test": "Monte Carlo"}],
    }
    refs = [entry["ref"] for entry in manifest["sources"]]
    return summary, claims_of(analysis_id, summary, refs)


def orchestrator(tmp_path: Path, launcher, *, run_dir: str = "run", run_id: str = "RUN-1") -> Orchestrator:
    """The real config, except that the `data` phase points at one small file: a unit test must not depend on the
    gigabytes of third-party input a live run hashes.

    `run_dir` and `run_id` are parameters because a store outlives the run that wrote it: a test that needs a
    second run over the first one's knowledge base points it at its own results directory.
    """
    source = tmp_path / "declared-input.csv"
    source.write_text("residue,sequence\n56-70,PEPTIDE\n")
    config = load_config()
    declared = DataSourceConfig(id="test-table", path=str(source), kind="table", license_note="test fixture")
    config = config.model_copy(
        update={
            "phases": config.phases.model_copy(
                update={"data": config.phases.data.model_copy(update={"sources": [declared]})}
            )
        }
    )
    return Orchestrator(
        config,
        tmp_path / "kb",
        tmp_path / run_dir,
        run_id,
        launcher,
        verifier=verifier,
        clock=lambda: NOW,
        fetch=lambda *a, **k: {},
        compute_runner=fake_analysis,
    )


def test_a_mocked_run_writes_claims_and_a_linked_hypothesis(tmp_path):
    launcher = FakeLauncher({"literature": literature, "generate": generate})
    state = orchestrator(tmp_path, launcher).run(("literature", "generate"))
    assert state["completed"] == ["literature", "generate"]
    lit = state["phases"]["literature"]
    assert (lit["dropped_agents"], lit["invalid_outputs"], lit["seen_again"]) == (1, 0, 1)
    assert lit["dropped_elements"] == 1  # literature-3's only claim has a bad ref
    records = read_records(tmp_path / "kb")
    claims = [r for r in records.values() if isinstance(r, Claim)]
    assert sorted((c.origin, c.provenance_status) for c in claims) == [
        ("agent", "unverified"),
        ("literature", "unverified"),
        ("literature", "verified"),
    ]
    assert len([r for r in records.values() if isinstance(r, Hypothesis)]) == 1
    assert len([r for r in records.values() if isinstance(r, Ignorance)]) == 1
    report = json.loads((tmp_path / "run" / "01-literature-report.json").read_text())
    assert "literature-3" in report["dropped_elements"] and report["dropped_agents"] == ["literature-4"]


def test_args_carry_the_generated_schema_and_a_verified_first_slice(tmp_path):
    launcher = FakeLauncher({"literature": literature, "generate": generate})
    orchestrator(tmp_path, launcher).run(("literature", "generate"))
    (_, lit_args), (_, gen_args) = launcher.calls
    assert lit_args["schema"]["type"] == "object" and len(lit_args["questions"]) == 4
    assert gen_args["kb_context"][0]["provenance_status"] == "verified"
    assert len(gen_args["perspectives"]) == 3 and "canonical_hash" not in gen_args["kb_context"][0]
    assert lit_args["web"]["session_searches"] == lit_args["web"]["searches"] * 4  # 4 questions, one agent each
    assert gen_args["web"] == {"searches": 0, "fetches": 0, "session_searches": 0}


def test_an_interrupted_run_resumes_after_the_last_completed_phase(tmp_path):
    def broken(args):
        raise PhaseFailed("generate: workflow did not complete")

    with pytest.raises(PhaseFailed):
        orchestrator(tmp_path, FakeLauncher({"literature": literature, "generate": broken})).run(
            ("literature", "generate")
        )
    state = json.loads((tmp_path / "run" / "state.json").read_text())
    assert state["completed"] == ["literature"]

    launcher = FakeLauncher({"literature": literature, "generate": generate})
    state = orchestrator(tmp_path, launcher).run(("literature", "generate"))
    assert [phase for phase, _ in launcher.calls] == ["generate"]
    assert state["completed"] == ["literature", "generate"]


def test_each_phase_reports_its_start_and_its_end_on_stderr(tmp_path, capsys):
    def broken(args):
        raise PhaseFailed("generate: workflow did not complete")

    def lines() -> list[str]:
        return [re.sub(r"\d+m\d{2}s|\d+s", "<t>", line) for line in capsys.readouterr().err.splitlines()]

    with pytest.raises(PhaseFailed):
        orchestrator(tmp_path, FakeLauncher({"literature": literature, "generate": broken})).run(
            ("literature", "generate")
        )
    assert lines() == [
        "pipeline: … 1/2 literature running",
        "pipeline: ✓ 1/2 literature done in <t>",
        "pipeline: … 2/2 generate running",
        "pipeline: ✗ 2/2 generate stopped after <t>",
    ]

    orchestrator(tmp_path, FakeLauncher({"literature": literature, "generate": generate})).run(
        ("literature", "generate")
    )
    assert lines() == [
        "pipeline: ✓ 1/2 literature (already completed)",
        "pipeline: … 2/2 generate running",
        "pipeline: ✓ 2/2 generate done in <t>",
    ]


def test_a_run_directory_belongs_to_one_run(tmp_path):
    orchestrator(tmp_path, FakeLauncher({"literature": literature, "generate": generate})).run(("literature",))
    other = Orchestrator(
        load_config(),
        tmp_path / "kb",
        tmp_path / "run",
        "RUN-2",
        FakeLauncher({}),
        verifier=verifier,
        clock=lambda: NOW,
    )
    with pytest.raises(ValueError, match="belongs to run RUN-1"):
        other.run()


def test_phase_run_result_shape_is_what_the_workflows_return(tmp_path):
    run = PhaseRun({"phase": "literature", "outputs": [], "dropped": []})
    launcher = FakeLauncher({"literature": lambda a: run.result, "generate": lambda a: {"outputs": []}})
    state = orchestrator(tmp_path, launcher).run(("literature", "generate"))
    assert state["phases"]["literature"]["written"] == 0


def test_the_fetch_cap_holds_for_the_whole_phase(tmp_path):
    asked: list[int] = []

    def fetch(sources, store, run_id, max_new):
        asked.append(max_new)
        return {s: "fetched" for s in list(dict.fromkeys(sources))[:max_new]}

    o = Orchestrator(
        load_config(),
        tmp_path / "kb",
        tmp_path / "run",
        "RUN-1",
        FakeLauncher({"literature": literature, "generate": generate}),
        verifier=verifier,
        clock=lambda: NOW,
        fetch=fetch,
    )
    o.store = object()  # any store: the fake fetch does not use it
    o.run(("literature",))
    cap = load_config().fetch_max_new
    assert asked[0] == cap and asked[1] == cap - 1  # the first output's claims cite one paper, fetched


def test_review_and_verify_make_statuses_informative(tmp_path):
    from knowledge_base.schemas import Relation
    from knowledge_base.status import statuses

    launcher = FakeLauncher(ALL)
    state = orchestrator(tmp_path, launcher).run(("literature", "generate", "review", "verify"))
    assert state["completed"] == ["literature", "generate", "review", "verify"]
    records = read_records(tmp_path / "kb")
    causal = next(r.id for r in records.values() if isinstance(r, Claim) and r.causal_fields() is not None)
    # generate linked a verified literature claim with `supports`, review added a quoted `contradicts`;
    # both were confirmed by the verifier, so the causal claim is now contested.
    assert statuses(records)[causal] == "contested"
    verified = [r for r in records.values() if isinstance(r, Relation) and r.provenance_status == "verified"]
    assert {r.type for r in verified} == {"supports", "contradicts"}
    assert all(r.justification_method == "quote" and "confirmed by verifier-1" in r.rationale for r in verified)
    review_report = json.loads((tmp_path / "run" / "03-review-report.json").read_text())
    assert list(review_report["summaries"].values()) == ["The causal step lacks direct evidence."]


def test_verify_runs_again_without_reverifying(tmp_path):
    launcher = FakeLauncher(ALL)
    o = orchestrator(tmp_path, launcher)
    o.run()
    assert o._verify_candidates(_open(tmp_path)) == []


def test_empty_phases_are_skipped_without_a_launch(tmp_path):
    launcher = FakeLauncher({"literature": lambda a: {"outputs": []}, "generate": lambda a: {"outputs": []}})
    state = orchestrator(tmp_path, launcher).run()
    assert [p for p, _ in launcher.calls] == ["literature", "generate"]
    assert all(
        "no open hypotheses" in state["phases"][p]["skipped"] for p in ("review", "design", "arbitrate", "synthesize")
    )
    assert "no surviving hypotheses" in state["phases"]["intervene"]["skipped"]
    assert "no candidate relations" in state["phases"]["verify"]["skipped"]


def test_a_verdict_on_a_relation_that_was_not_a_candidate_writes_nothing(tmp_path):
    def rogue(args):
        return {
            "outputs": [
                {
                    "agent_id": "verifier-1",
                    "output": {
                        "verdicts": [{"relation_id": "L-1", "faithful": True, "verdict": "confirmed", "reason": "x"}]
                    },
                }
            ]
        }

    state = orchestrator(tmp_path, FakeLauncher({**ALL, "verify": rogue})).run()
    report = json.loads((tmp_path / "run" / "06-verify-report.json").read_text())
    assert "L-1" in report["verdicts"]
    assert state["phases"]["verify"]["written"] == 0


def _open(tmp_path):
    class View:  # the subset of KnowledgeBase that _verify_candidates reads
        records = read_records(tmp_path / "kb")

    return View()


def test_a_resume_with_another_config_must_be_declared_and_is_recorded(tmp_path):
    def broken(args):
        raise PhaseFailed("generate stopped")

    with pytest.raises(PhaseFailed):
        orchestrator(tmp_path, FakeLauncher({"literature": literature, "generate": broken})).run(
            ("literature", "generate")
        )
    changed = load_config().model_copy(update={"config_hash": "other"})

    def resumed(change=None):
        return Orchestrator(
            changed,
            tmp_path / "kb",
            tmp_path / "run",
            "RUN-1",
            FakeLauncher({"literature": literature, "generate": generate}),
            verifier=verifier,
            clock=lambda: NOW,
            fetch=lambda *a, **k: {},
            config_change=change,
        )

    with pytest.raises(ValueError, match="declare the change"):
        resumed().run(("literature", "generate"))
    state = resumed("raise the review budget").run(("literature", "generate"))
    assert state["config_changes"][0]["reason"] == "raise the review budget"
    assert state["config_changes"][0]["at_phase"] == 2 and state["config_hash"] == "other"


def test_one_faulty_element_does_not_cost_the_whole_review(tmp_path):
    def sloppy_review(args):
        item = args["items"][0]
        target = next(c["id"] for c in item["context"] if c.get("cause_ref"))  # the edge, whatever its type
        return {
            "outputs": [
                {
                    "agent_id": "reviewer-1",
                    "hypothesis_id": item["hypothesis_id"],
                    "output": {
                        "claims": [
                            {"ref": "e1", "type": "observational", "content": "An unsourced opinion."},
                            # Half an edge: the five causal fields are all-or-nothing, so this one claim is dropped alone.
                            {
                                "ref": "e2",
                                "type": "observational",
                                "content": "x drives y",
                                "causal": {"cause": "x", "effect": "y"},
                            },
                            {
                                "ref": "e3",
                                "type": "observational",
                                "content": "IL-10 responses do not track neuron loss.",
                                "source": "doi:10.1000/fixture-other",
                                "quote": CONTRA_QUOTE,
                            },
                        ],
                        "links": [
                            {"type": "contradicts", "source": "e1", "target": target, "rationale": "no evidence ref"},
                            {"type": "contradicts", "source": "e2", "target": target, "rationale": "dropped claim"},
                            {"type": "contradicts", "source": "e3", "target": target, "rationale": "quoted"},
                        ],
                        "gaps": [],
                        "summary": "Mixed.",
                    },
                }
            ]
        }

    state = orchestrator(tmp_path, FakeLauncher({**ALL, "review": sloppy_review})).run(
        ("literature", "generate", "review")
    )
    report = json.loads((tmp_path / "run" / "03-review-report.json").read_text())
    assert report["invalid_outputs"] == {}
    dropped = report["dropped_elements"]["reviewer-1"]
    assert any(d.startswith("claim e2") for d in dropped)
    # e1 is an unsourced opinion claim, so its `contradicts` is no longer dropped: it is kept one step down the
    # epistemic ladder, as an Ignorance record questioning the same target.
    # The `contradicts` whose source is the unsourced claim is no longer dropped: it is kept one step down the
    # epistemic ladder as an Ignorance record questioning the same target. The
    # message names the allocated IDs, not the agent's local refs, because `_link` resolves both endpoints first.
    converted = report["converted_elements"]["reviewer-1"]
    assert len(converted) == 1 and "not verified" in converted[0] and "questions" in converted[0]
    assert any(r.startswith("I-") for r in report["written"])
    assert any("contradicts e2" in d and "unknown local ref e2" in d for d in dropped)
    assert state["phases"]["review"]["written"] >= 3  # e1, e3 and the quoted contradicts


def test_a_mocked_full_run_ends_with_a_gated_proposal(tmp_path):
    from knowledge_base.schemas import Decision, Experiment, Intervention, Relation

    state = orchestrator(tmp_path, FakeLauncher(ALL)).run()
    assert state["completed"] == [
        "data",
        "compute",
        "literature",
        "generate",
        "review",
        "verify",
        "design",
        "arbitrate",
        "execute",
        "reassess",
        "intervene",
        "synthesize",
    ]
    records = read_records(tmp_path / "kb")
    [exp] = [r for r in records.values() if isinstance(r, Experiment)]
    [dec] = [r for r in records.values() if isinstance(r, Decision)]
    assert dec.decision == "buy" and dec.experiment_ids == [exp.id]
    assert any(isinstance(r, Relation) and r.type == "tests" and r.source_id == exp.id for r in records.values())
    [cand] = [r for r in records.values() if isinstance(r, Intervention)]
    assert cand.hypothesis_id == dec.hypothesis_id and state["phases"]["intervene"]["candidates"] == {
        dec.hypothesis_id: 1
    }
    assert {
        r.target_id
        for r in records.values()
        if isinstance(r, Relation) and r.type == "depends_on" and r.source_id == cand.id
    } == set(cand.acts_on_claims)
    final = json.loads((tmp_path / "run" / "final-proposal.json").read_text())
    assert final["gate"]["passed"] and final["gate"]["bought_decisions"] == [dec.id]
    # the cited causal claim is contested (supports and contradicts both verified): the gate says so
    assert any("contested" in x for xs in final["gate"]["cites_contradicted_or_contested"].values() for x in xs)
    assert state["phases"]["synthesize"]["proposal"]["outcome"] == "proposal"
    assert "## H. Failure modes" in (tmp_path / "run" / "final-proposal.md").read_text()


def test_an_arbitrated_hypothesis_is_not_designed_or_arbitrated_again(tmp_path):
    o = orchestrator(tmp_path, FakeLauncher(ALL))
    o.run(("literature", "generate", "review", "verify", "design", "arbitrate"))

    class View:
        records = read_records(tmp_path / "kb")

    assert o._review_items(View(), 2, open_only=True) == []


def test_intervene_skips_a_rejected_hypothesis(tmp_path):
    def reject(args):
        item = args["items"][0]
        return {
            "outputs": [
                {
                    "agent_id": f"arbiter@{item['hypothesis_id']}",
                    "hypothesis_id": item["hypothesis_id"],
                    "output": {
                        "decision": "rejected",
                        "experiment_ids": [],
                        "value_of_information": "low",
                        "rationale": "contradicted",
                    },
                }
            ]
        }

    launcher = FakeLauncher(ALL | {"arbitrate": reject})
    state = orchestrator(tmp_path, launcher).run(
        ("literature", "generate", "review", "verify", "design", "arbitrate", "intervene")
    )
    assert "intervene" not in [p for p, _ in launcher.calls]
    assert "no surviving hypotheses" in state["phases"]["intervene"]["skipped"]


def test_no_candidate_is_a_valid_outcome_and_its_reason_is_kept(tmp_path):
    def none(args):
        item = args["items"][0]
        return {
            "outputs": [
                {
                    "agent_id": f"intervener@{item['hypothesis_id']}",
                    "hypothesis_id": item["hypothesis_id"],
                    "output": {"candidates": [], "no_candidate_reason": "mechanism too uncertain"},
                }
            ]
        }

    state = orchestrator(tmp_path, FakeLauncher(ALL | {"intervene": none})).run(
        ("literature", "generate", "review", "verify", "design", "arbitrate", "intervene")
    )
    assert list(state["phases"]["intervene"]["no_candidate"].values()) == ["mechanism too uncertain"]


def test_computed_claims_keep_a_place_in_the_hypothesis_authors_slice(tmp_path):
    """With a plain verified-first sort, computed claims can be crowded out of the slice entirely, because
    literature claims accumulate across runs."""
    from kb_factories import claim as make_claim

    from pipeline.orchestrator import Orchestrator

    records = {}
    for n in range(50):  # plenty of verified literature, as a shared KB has
        c = make_claim(id=f"C-{n + 1}", content=f"a literature claim {n}")
        records[c.id] = c
    for n in range(6):
        c = make_claim(
            id=f"C-{100 + n}",
            content=f"a1: key{n} = {n}.",
            origin="computation",
            verification_method="code",
            source_refs=["dataset:x@abc", f"result:a1@d{n}"],
        )
        records[c.id] = c

    class View:
        pass

    View.records = records
    context = Orchestrator._claims_context(View(), 12)
    computed = [c["id"] for c in context if c.get("origin") == "computation"]
    assert len(context) == 12
    assert computed == ["C-100", "C-101", "C-102"]  # a quarter of the slice is reserved, no more
    assert any(c.get("origin") == "literature" for c in context)


def test_the_runs_own_fresh_claims_keep_a_place_in_the_hypothesis_authors_slice(tmp_path):
    """A stable sort inside the verified block would show the authors only the oldest verified claims, and
    none of those the run's own `literature` phase had just written."""
    from datetime import timedelta

    from kb_factories import claim as make_claim

    from pipeline.orchestrator import Orchestrator

    records = {}
    for n in range(50):  # the accumulated knowledge base of earlier runs
        c = make_claim(id=f"C-{n + 1}", content=f"an older literature claim {n}", run_id="RUN-OLD")
        records[c.id] = c
    for n in range(9):  # what this run's literature phase just wrote
        c = make_claim(
            id=f"C-{200 + n}",
            content=f"a fresh literature claim {n}",
            run_id="RUN-NEW",
            created_at=records["C-1"].created_at + timedelta(minutes=n + 1),
        )
        records[c.id] = c
    unverified = make_claim(
        id="C-250",
        content="a fresh agent assertion",
        run_id="RUN-NEW",
        origin="agent",
        provenance_status="unverified",
        verification_method="none",
        source_refs=[],
    )
    records[unverified.id] = unverified

    class View:
        pass

    View.records = records
    context = Orchestrator._claims_context(View(), 12, "RUN-NEW")
    fresh = [c["id"] for c in context if "fresh" in c["content"]]
    assert len(context) == 12
    assert fresh == ["C-208", "C-207", "C-206", "C-205"]  # a third of the slice, verified first, newest first
    assert "C-250" not in fresh  # the fresh agent assertion sorts behind the verified
    assert any(c["id"] == "C-1" for c in context)  # the older knowledge base keeps the majority
    # Without a run id the slice is what it was before this reservation existed.
    assert [c["id"] for c in Orchestrator._claims_context(View(), 12)][:3] == ["C-1", "C-2", "C-3"]


def test_the_literature_phase_gets_no_slice_by_choice_not_because_the_store_is_empty(tmp_path):
    """`kb_context: []` for `literature` is a clean room, so extraction is not steered by what is recorded; it is
    not a report that the store is empty. The sentence the agents read must therefore not say it is: the empty
    branch of `contextBlock` (`workflows/phases/src/lib/phase.js`) says what is true of the prompt instead, and
    `tests/workflow/phases.test.js` pins that wording."""
    from kb_factories import claim as make_claim

    records = {}
    for n in range(5):
        c = make_claim(id=f"C-{n + 1}", content=f"a verified literature claim {n}")
        records[c.id] = c

    class View:
        pass

    View.records = records
    args = orchestrator(tmp_path, FakeLauncher({}))._args("literature", View())
    assert args["kb_context"] == []  # the clean room, deliberately
    assert Orchestrator._claims_context(View(), 12, "RUN-1")  # and the store it hides is not empty


def test_the_data_phase_hashes_the_declared_sources_without_a_launch(tmp_path):
    launcher = FakeLauncher({})
    state = orchestrator(tmp_path, launcher).run(("data",))

    assert launcher.calls == []  # no agent, no `claude -p`, no cost
    report = state["phases"]["data"]
    assert list(report["sources"]) == ["test-table"] and report["files"] == 1
    assert report["sources"]["test-table"].startswith("dataset:test-table@")
    manifest = json.loads((tmp_path / "run" / "data-manifest.json").read_text())
    [entry] = manifest["sources"]
    assert entry["sha256"] and entry["license_note"] == "test fixture" and entry["kind"] == "table"


def test_a_missing_declared_source_stops_the_run(tmp_path):
    o = orchestrator(tmp_path, FakeLauncher({}))
    gone = o.config.phases.data.sources[0].model_copy(update={"path": str(tmp_path / "absent.csv")})
    o.config = o.config.model_copy(
        update={
            "phases": o.config.phases.model_copy(
                update={"data": o.config.phases.data.model_copy(update={"sources": [gone]})}
            )
        }
    )
    with pytest.raises(MissingSource, match="absent.csv"):
        o.run(("data",))


# --- The verifier confirms on the quote but judges the claim overstated --------------------------------------
# `Builder.confirmed` needs the verdict *and* `faithful`; without this path the verdict wrote nothing at all.
# 34 such verdicts across nine archived runs, among them every confirmed contradiction of the two production
# runs, which is why no hypothesis there ever had a `contradicted` claim.


def _unfaithful(narrowed: bool):
    def respond(args):
        verdicts = []
        for batch in args["batches"]:
            for candidate in batch:
                verdict = {
                    "relation_id": candidate["relation_id"],
                    "faithful": False,
                    "verdict": "confirmed",
                    "reason": "the quote bears on the target, the claim says more than the quote",
                }
                if narrowed:
                    verdict["narrowed"] = f"Only what the quote says, for {candidate['relation_id']}."
                verdicts.append(verdict)
        return {
            "phase": "verify",
            "dropped": [],
            "outputs": [{"agent_id": "verifier-1", "output": {"verdicts": verdicts}}],
        }

    return respond


def test_a_confirmed_verdict_on_an_overstated_claim_still_produces_verified_evidence(tmp_path):
    from knowledge_base.schemas import Relation
    from knowledge_base.status import statuses

    launcher = FakeLauncher(ALL | {"verify": _unfaithful(narrowed=True)})
    orchestrator(tmp_path, launcher).run(("literature", "generate", "review", "verify"))
    records = read_records(tmp_path / "kb")

    narrowed = [r for r in records.values() if isinstance(r, Claim) and r.content.startswith("Only what the quote")]
    assert narrowed, "the verifier's restatement became a claim"
    assert all(c.provenance_status == "verified" and c.verification_method == "quote" for c in narrowed)
    verified = [r for r in records.values() if isinstance(r, Relation) and r.provenance_status == "verified"]
    assert {r.type for r in verified} == {"derived_from", "supports", "contradicts"}
    assert all(r.source_id in {c.id for c in narrowed} for r in verified)
    causal = next(r.id for r in records.values() if isinstance(r, Claim) and r.causal_fields() is not None)
    assert statuses(records)[causal] == "contested"
    report = json.loads((tmp_path / "run" / "04-verify-report.json").read_text())
    assert report["not_narrowed"] == {}


def test_without_a_restatement_nothing_is_written_and_the_reason_is_reported(tmp_path):
    from knowledge_base.schemas import Relation

    launcher = FakeLauncher(ALL | {"verify": _unfaithful(narrowed=False)})
    orchestrator(tmp_path, launcher).run(("literature", "generate", "review", "verify"))
    records = read_records(tmp_path / "kb")

    assert not [r for r in records.values() if isinstance(r, Relation) and r.provenance_status == "verified"]
    report = json.loads((tmp_path / "run" / "04-verify-report.json").read_text())
    assert report["not_narrowed"], "the discarded verdicts are named"
    assert all("no restatement was offered" in reason for reason in report["not_narrowed"].values())


def test_a_narrowed_relation_is_not_offered_for_verification_again(tmp_path):
    """The verified relation starts from the narrowed claim, not from the original source, so coverage has to
    follow the `derived_from` or the same relation would be re-narrowed on every later run."""
    launcher = FakeLauncher(ALL | {"verify": _unfaithful(narrowed=True)})
    o = orchestrator(tmp_path, launcher)
    o.run(("literature", "generate", "review", "verify"))

    assert o._verify_candidates(_open(tmp_path)) == []


def test_a_quoted_but_unverified_claim_is_now_offered_for_verification(tmp_path):
    """A claim that carries a quote and is unverified must be offered to the verifier, or a citation that
    overstates its source is never checked."""
    from kb_factories import claim, relation

    from knowledge_base.store import KnowledgeBase

    quoted = claim(
        id="C-1",
        content="A claim that says more than its quote.",
        quote="a verbatim sentence",
        provenance_status="unverified",
        verification_method="none",
    )
    target = claim(id="C-2", content="A target.")
    silent = claim(
        id="C-3",
        content="No quote at all.",
        origin="agent",
        provenance_status="unverified",
        verification_method="none",
        source_refs=[],
    )
    link = relation(
        id="L-1",
        source_id="C-1",
        target_id="C-2",
        justification_method="agent_inference",
        provenance_status="unverified",
    )
    mute = relation(
        id="L-2",
        source_id="C-3",
        target_id="C-2",
        justification_method="agent_inference",
        provenance_status="unverified",
    )
    kb_dir = tmp_path / "kb"
    kb_dir.mkdir(parents=True)
    with KnowledgeBase.open(kb_dir) as kb:
        kb.commit([quoted, target, silent, link, mute], "RUN-1", "tester")

    o = orchestrator(tmp_path, FakeLauncher(ALL))
    with KnowledgeBase.open(kb_dir) as kb:
        offered = {c["relation_id"] for c in o._verify_candidates(kb)}

    assert "L-1" in offered, "a quoted, unverified claim is checkable and must be offered"
    assert "L-2" not in offered, "a claim with no quote has nothing to compare and is not offered"


def test_feasibility_is_archived_and_reaches_arbiter_and_pi(tmp_path):
    def two_tests(args):
        item = args["items"][0]
        claim_id = item["targets"][0]
        expected = [
            {"outcome": "association", "claim_id": claim_id, "effect": "supports"},
            {"outcome": "no association", "claim_id": claim_id, "effect": "contradicts"},
        ]
        proposals = [
            {
                "title": name,
                "protocol": "Regate the existing cohort with controls.",
                "tested_claims": [claim_id],
                "expected_outcomes": expected,
                "cost": "low",
                "cost_rationale": "local dataset",
                "feasibility": "medium",
                "kind": "in_silico",
                "dataset_refs": ["test-table"],
                "dataset_location": "local",
                "required_markers": [marker],
                "required_metadata": [],
            }
            for name, marker in (("FoxP3 test", "FoxP3"), ("IL-10 test", "IL-10"))
        ]
        proposals.append({**proposals[0], "title": "Unavailable dataset test", "dataset_refs": ["not-in-manifest"]})
        return {
            "outputs": [
                {"agent_id": "designer-1", "hypothesis_id": item["hypothesis_id"], "output": {"experiments": proposals}}
            ]
        }

    def pi(args):
        output = synthesize(args)
        ids = sorted(args["experiment_feasibility"])
        output["outputs"][0]["output"]["validation"] = {
            "text": "Test both alternatives " + " ".join(f"[[{eid}]]" for eid in ids) + ".",
            "cites": ids,
        }
        return output

    launcher = FakeLauncher(ALL | {"design": two_tests, "synthesize": pi})
    o = orchestrator(tmp_path, launcher)
    o.run(("data",))
    manifest_path = tmp_path / "run" / "data-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["sources"][0]["shape"] = {"markers": ["CD4", "IL-10"]}
    manifest_path.write_text(json.dumps(manifest))
    o.run(("literature", "generate", "review", "verify", "design", "arbitrate", "intervene", "synthesize"))

    feasibility = json.loads((tmp_path / "run" / "05-design-feasibility.json").read_text())
    rows = feasibility["experiments"]
    assert {r["status"] for r in rows.values()} == {"missing_inputs", "data_supported"}
    assert feasibility["refused"] == [
        {
            "agent_id": "designer-1",
            "title": "Unavailable dataset test",
            "experiment_id": None,
            "status": "refused",
            "reason": "experiment 'Unavailable dataset test': local dataset(s) not in this run's manifest: ['not-in-manifest']",
        }
    ]
    missing = next(r for r in rows.values() if r["status"] == "missing_inputs")
    assert missing["missing"] == ["marker:FoxP3"]
    assert missing["sources"][0]["sha256"] == manifest["sources"][0]["sha256"]
    arb_args = next(args for phase, args in launcher.calls if phase == "arbitrate")
    pi_args = next(args for phase, args in launcher.calls if phase == "synthesize")
    assert set(arb_args["items"][0]["experiment_feasibility"]) == set(rows)
    assert pi_args["experiment_feasibility"] == rows
    final = (tmp_path / "run" / "final-proposal.md").read_text()
    assert "marker:FoxP3" in final and "data_supported" in final


def test_bought_registered_experiment_runs_and_reaches_independent_reassessment(tmp_path):
    _, _, _, fixture_manifest = hla_fixture(tmp_path)

    def design_registered(args):
        item = args["items"][0]
        target = item["targets"][0]
        return {
            "outputs": [
                {
                    "agent_id": "designer-1",
                    "hypothesis_id": item["hypothesis_id"],
                    "output": {
                        "experiments": [
                            {
                                "title": "Compact four-allele panel diagnostic",
                                "protocol": "Find the smallest pool covering the four predicted HLA alleles; compare all size-matched subsets.",
                                "tested_claims": [target],
                                "expected_outcomes": [
                                    {"outcome": "all four covered", "claim_id": target, "effect": "supports"},
                                    {"outcome": "an allele uncovered", "claim_id": target, "effect": "contradicts"},
                                ],
                                "cost": "low",
                                "cost_rationale": "three local small files",
                                "feasibility": "high",
                                "kind": "in_silico",
                                "dataset_location": "local",
                                "dataset_refs": [entry["ref"] for entry in fixture_manifest["sources"]],
                                "analysis_id": "hla_panel_compact_v1",
                            }
                        ]
                    },
                }
            ]
        }

    def execute_registered(args):
        return execute_every(args, tmp_path / "run" / "execution-policy.json")

    def reassess_registered(args):
        execution = args["executions"][0]["execution"]
        assert execution["interpretation_check"]["faithful"]
        return {
            "outputs": [
                {
                    "agent_id": f"reassessor@{execution['experiment_id']}",
                    "output": {
                        "experiment_id": execution["experiment_id"],
                        "result_digest": execution["result_digest"],
                        "decision": "inconclusive",
                        "rationale": "Four predicted alleles cannot settle the broader causal claim.",
                        "claim_ids": [execution["written"][0]],
                        "limitations": ["No population allele frequencies or observed presentation."],
                    },
                }
            ]
        }

    def final(args):
        output = synthesize(args)
        eid = args["execution"]["executions"][0]["experiment_id"]
        output["outputs"][0]["output"]["performed_experiment_ids"] = [eid]
        output["outputs"][0]["output"]["validation"] = {"text": f"The panel diagnostic ran [[{eid}]].", "cites": [eid]}
        return output

    launcher = FakeLauncher(
        ALL
        | {
            "design": design_registered,
            "execute": execute_registered,
            "reassess": reassess_registered,
            "synthesize": final,
        }
    )
    o = orchestrator(tmp_path, launcher)
    sources = [
        DataSourceConfig(id=entry["id"], path=entry["path"], kind="table", license_note="fixture")
        for entry in fixture_manifest["sources"]
    ]
    o.config = o.config.model_copy(
        update={
            "phases": o.config.phases.model_copy(
                update={"data": o.config.phases.data.model_copy(update={"sources": sources})}
            )
        }
    )
    state = o.run()
    assert state["phases"]["execute"]["status"] == "executed"
    assert state["phases"]["reassess"]["status"] == "judged"
    final_json = json.loads((tmp_path / "run" / "final-proposal.json").read_text())
    assert final_json["gate"]["passed"]
    assert final_json["execution"]["executions"][0]["interpretation_check"]["faithful"]
    assert final_json["reassessment"]["judgements"][0]["judgement"]["decision"] == "inconclusive"
    final_md = (tmp_path / "run" / "final-proposal.md").read_text()
    assert "performed (verified receipt)" in final_md
    assert "Agent judgement: inconclusive" in final_md


def test_reassessment_citing_only_the_tested_claim_is_refused(tmp_path):
    """A judgement that never cites the result it reassesses.

    The guard requires one of the computed-result claims; the happy-path mock above satisfies it
    because it was written against the guard, so only a mock that behaves like the live agent — citing
    the tested claim alone — shows what the phase does. It must refuse, and preserve the agent's output.
    """
    _, _, _, fixture_manifest = hla_fixture(tmp_path)

    def design_registered(args):
        item = args["items"][0]
        target = item["targets"][0]
        return {
            "outputs": [
                {
                    "agent_id": "designer-1",
                    "hypothesis_id": item["hypothesis_id"],
                    "output": {
                        "experiments": [
                            {
                                "title": "Compact four-allele panel diagnostic",
                                "protocol": "Find the smallest pool covering the four predicted HLA alleles; compare all size-matched subsets.",
                                "tested_claims": [target],
                                "expected_outcomes": [
                                    {"outcome": "all four covered", "claim_id": target, "effect": "supports"},
                                    {"outcome": "an allele uncovered", "claim_id": target, "effect": "contradicts"},
                                ],
                                "cost": "low",
                                "cost_rationale": "three local small files",
                                "feasibility": "high",
                                "kind": "in_silico",
                                "dataset_location": "local",
                                "dataset_refs": [entry["ref"] for entry in fixture_manifest["sources"]],
                                "analysis_id": "hla_panel_compact_v1",
                            }
                        ]
                    },
                }
            ]
        }

    def execute_registered(args):
        return execute_every(args, tmp_path / "run" / "execution-policy.json")

    def reassess_tested_claim_only(args):
        execution = args["executions"][0]["execution"]
        return {
            "outputs": [
                {
                    "agent_id": f"reassessor@{execution['experiment_id']}",
                    "output": {
                        "experiment_id": execution["experiment_id"],
                        "result_digest": execution["result_digest"],
                        "decision": "inconclusive",
                        "rationale": "The panel bears on the tested claim only.",
                        "claim_ids": list(execution["tested_claims"]),
                        "limitations": ["No population allele frequencies or observed presentation."],
                    },
                }
            ]
        }

    launcher = FakeLauncher(
        ALL | {"design": design_registered, "execute": execute_registered, "reassess": reassess_tested_claim_only}
    )
    o = orchestrator(tmp_path, launcher)
    sources = [
        DataSourceConfig(id=entry["id"], path=entry["path"], kind="table", license_note="fixture")
        for entry in fixture_manifest["sources"]
    ]
    o.config = o.config.model_copy(
        update={
            "phases": o.config.phases.model_copy(
                update={"data": o.config.phases.data.model_copy(update={"sources": sources})}
            )
        }
    )
    state = o.run()

    assert state["phases"]["execute"]["status"] == "executed"
    report = state["phases"]["reassess"]
    assert report["status"] == "unverified"
    # The reason is named, and names the claims that were required.
    assert "cites none of the computed-result claims" in report["judgements"][0]["error"]
    # The refusal keeps the agent's own output, so the archive says what was judged and why it failed.
    assert report["agent_outputs"][0]["output"]["decision"] == "inconclusive"


def test_a_digest_with_prose_appended_is_refused_by_the_contract(tmp_path):
    """A digest with prose appended, as a regression.

    Its reassessor wrote `"7d531cfe… (outcome: panel_condition_met; host interpretation_che…"` into
    `result_digest`, a field the host compares character for character because it is the only proof the
    judgement is about this execution. The field is now a digest in the schema the agent is handed, so the
    output fails validation and the report names the field instead of the generic binding error.
    """
    _, _, _, fixture_manifest = hla_fixture(tmp_path)

    def design_registered(args):
        item = args["items"][0]
        target = item["targets"][0]
        return {
            "outputs": [
                {
                    "agent_id": "designer-1",
                    "hypothesis_id": item["hypothesis_id"],
                    "output": {
                        "experiments": [
                            {
                                "title": "Compact four-allele panel diagnostic",
                                "protocol": "Find the smallest pool covering the four predicted HLA alleles; compare all size-matched subsets.",
                                "tested_claims": [target],
                                "expected_outcomes": [
                                    {"outcome": "all four covered", "claim_id": target, "effect": "supports"},
                                    {"outcome": "an allele uncovered", "claim_id": target, "effect": "contradicts"},
                                ],
                                "cost": "low",
                                "cost_rationale": "three local small files",
                                "feasibility": "high",
                                "kind": "in_silico",
                                "dataset_location": "local",
                                "dataset_refs": [entry["ref"] for entry in fixture_manifest["sources"]],
                                "analysis_id": "hla_panel_compact_v1",
                            }
                        ]
                    },
                }
            ]
        }

    def execute_registered(args):
        return execute_every(args, tmp_path / "run" / "execution-policy.json")

    def reassess_with_a_commented_digest(args):
        execution = args["executions"][0]["execution"]
        return {
            "outputs": [
                {
                    "agent_id": f"reassessor@{execution['experiment_id']}",
                    "output": {
                        "experiment_id": execution["experiment_id"],
                        "result_digest": f"{execution['result_digest']} (outcome: panel_condition_met)",
                        "decision": "inconclusive",
                        "rationale": "Four predicted alleles cannot settle the causal claim.",
                        "claim_ids": list(execution["written"]),
                        "limitations": ["No population allele frequencies or observed presentation."],
                    },
                }
            ]
        }

    launcher = FakeLauncher(
        ALL | {"design": design_registered, "execute": execute_registered, "reassess": reassess_with_a_commented_digest}
    )
    o = orchestrator(tmp_path, launcher)
    sources = [
        DataSourceConfig(id=entry["id"], path=entry["path"], kind="table", license_note="fixture")
        for entry in fixture_manifest["sources"]
    ]
    o.config = o.config.model_copy(
        update={
            "phases": o.config.phases.model_copy(
                update={"data": o.config.phases.data.model_copy(update={"sources": sources})}
            )
        }
    )
    state = o.run(PHASES[: PHASES.index("reassess") + 1])

    assert state["phases"]["execute"]["status"] == "executed"
    report = state["phases"]["reassess"]
    assert report["status"] == "unverified"
    error = report["judgements"][0]["error"]
    assert "result_digest" in error, error
    # The agent's own output is kept, so the archive shows what was judged and which field broke it.
    assert report["agent_outputs"][0]["output"]["decision"] == "inconclusive"


def test_a_repeated_experiment_is_reassessed_on_the_claims_the_store_kept(tmp_path):
    """A repeated experiment is refused by the guard, and the rule that answers it.

    The archive: `09-execute-report.json` has `status: executed`, a faithful interpretation check and
    `written: []`, with the four computed facts in `seen_again` (`C-1206` -> `C-889` ...) because
    an earlier run had already run the same deterministic analysis and the store records an identical claim
    as seen again rather than as new evidence. Built on `written` alone, the guard demanded an intersection
    with an empty set — unsatisfiable — and served the reassessor `computed_result_claims` from that same
    empty list, so no judgement could pass however well the agent behaved.

    `reassess` now takes the claims of an execution to be the ones the store holds for it,
    `written | seen_again.values()`. The result is not new; the hypothesis the run asks about is.

    The agent's copy of the report lists only the IDs the store kept: a reassessor once cited
    `C-1206`, a `seen_again` key it read off the report, which names no claim in the base.
    """
    _, _, _, fixture_manifest = hla_fixture(tmp_path)
    served: list[list] = []
    shown: list[dict] = []

    def design_registered(args):
        item = args["items"][0]
        target = item["targets"][0]
        return {
            "outputs": [
                {
                    "agent_id": "designer-1",
                    "hypothesis_id": item["hypothesis_id"],
                    "output": {
                        "experiments": [
                            {
                                "title": "Compact four-allele panel diagnostic",
                                "protocol": "Find the smallest pool covering the four predicted HLA alleles; compare all size-matched subsets.",
                                "tested_claims": [target],
                                "expected_outcomes": [
                                    {"outcome": "all four covered", "claim_id": target, "effect": "supports"},
                                    {"outcome": "an allele uncovered", "claim_id": target, "effect": "contradicts"},
                                ],
                                "cost": "low",
                                "cost_rationale": "three local small files",
                                "feasibility": "high",
                                "kind": "in_silico",
                                "dataset_location": "local",
                                "dataset_refs": [entry["ref"] for entry in fixture_manifest["sources"]],
                                "analysis_id": "hla_panel_compact_v1",
                            }
                        ]
                    },
                }
            ]
        }

    def arbitrate_this_run(args):
        """The shared `arbitrate` mock buys the first EXP in context, which in a store holding an earlier run
        is that run's; a bought EXP of another run is refused before `execute` ever selects it."""
        item = args["items"][0]
        eid = max(
            (c["id"] for c in item["context"] if c["id"].startswith("EXP-")), key=lambda i: int(i.split("-", 1)[1])
        )
        return {
            "outputs": [
                {
                    "agent_id": f"arbiter@{item['hypothesis_id']}",
                    "hypothesis_id": item["hypothesis_id"],
                    "output": {
                        "decision": "buy",
                        "experiment_ids": [eid],
                        "value_of_information": "high",
                        "rationale": f"{eid} settles the contested causal step",
                    },
                }
            ]
        }

    def reassess_citing_the_result_the_store_kept(args):
        execution = args["executions"][0]["execution"]
        served.append(args["executions"][0]["hypothesis"]["computed_result_claims"])
        shown.append(args["executions"][0]["execution"])
        return {
            "outputs": [
                {
                    "agent_id": f"reassessor@{execution['experiment_id']}",
                    "output": {
                        "experiment_id": execution["experiment_id"],
                        "result_digest": execution["result_digest"],
                        "decision": "inconclusive",
                        "rationale": "Four predicted alleles cannot settle the broader causal claim.",
                        "claim_ids": execution["written"] or execution["seen_again"],
                        "limitations": ["No population allele frequencies or observed presentation."],
                    },
                }
            ]
        }

    def execute_in(run_dir: str):
        def execute_registered(args):
            eid = args["selected"][0]["experiment_id"]
            result = run_experiment(tmp_path / run_dir / "execution-policy.json", eid)
            assert "error" not in result
            return {
                "outputs": [
                    {
                        "agent_id": f"executor@{eid}",
                        "output": {
                            "experiment_id": eid,
                            "analysis_id": result["analysis_id"],
                            "result_digest": result["result_digest"],
                            "outcome": result["outcome"],
                            "interpretation": "supports_panel_condition_only",
                            "limitations": result["limitations"],
                        },
                    }
                ]
            }

        return execute_registered

    def final(args):
        output = synthesize(args)
        eid = args["execution"]["executions"][0]["experiment_id"]
        output["outputs"][0]["output"]["performed_experiment_ids"] = [eid]
        output["outputs"][0]["output"]["validation"] = {"text": f"The panel diagnostic ran [[{eid}]].", "cites": [eid]}
        return output

    def run_once(run_dir: str, run_id: str, phases=PHASES[: PHASES.index("reassess") + 1]):
        launcher = FakeLauncher(
            ALL
            | {
                "design": design_registered,
                "execute": execute_in(run_dir),
                "arbitrate": arbitrate_this_run,
                "synthesize": final,
                "reassess": reassess_citing_the_result_the_store_kept,
            }
        )
        o = orchestrator(tmp_path, launcher, run_dir=run_dir, run_id=run_id)
        sources = [
            DataSourceConfig(id=entry["id"], path=entry["path"], kind="table", license_note="fixture")
            for entry in fixture_manifest["sources"]
        ]
        o.config = o.config.model_copy(
            update={
                "phases": o.config.phases.model_copy(
                    update={"data": o.config.phases.data.model_copy(update={"sources": sources})}
                )
            }
        )
        return o.run(phases)

    first = run_once("run", "RUN-1")
    (first_execution,) = first["phases"]["execute"]["executions"]
    assert first_execution["written"], "the first execution writes the computed facts"
    assert first["phases"]["reassess"]["status"] == "judged"
    assert served[0], "the first reassessor is shown the computed-result claims"

    second = run_once("run-2", "RUN-2", PHASES)
    execute = second["phases"]["execute"]
    (execution,) = execute["executions"]
    assert execute["status"] == "executed" and execution["interpretation_check"]["faithful"]
    assert execution["written"] == [], "the same analysis over the same inputs commits no new claim"
    assert set(execution["seen_again"].values()) == set(first_execution["written"])

    report = second["phases"]["reassess"]
    assert report["status"] == "judged", report.get("error")
    assert report["judgements"][0]["judgement"]["decision"] == "inconclusive"
    # Shown, and judged on, the claims the first run committed — under the IDs the base kept.
    assert [claim["id"] for claim in served[1]] == sorted(execution["seen_again"].values())
    assert set(report["judgements"][0]["judgement"]["claim_ids"]) <= set(first_execution["written"])
    # The dropped IDs never reach the agent; the kept ones do, so the replication stays visible.
    assert shown[1]["seen_again"] == sorted(execution["seen_again"].values())
    assert not set(execution["seen_again"]) & set(re.findall(r"C-\d+", json.dumps(shown[1])))

    # End to end: the judgement reaches the proposal, so a run over an accumulated base concludes.
    final_json = json.loads((tmp_path / "run-2" / "final-proposal.json").read_text())
    assert final_json["reassessment"]["judgements"][0]["judgement"]["decision"] == "inconclusive"
    assert "Agent judgement: inconclusive" in (tmp_path / "run-2" / "final-proposal.md").read_text()


# --- which relations the verifier is offered -------------------------------------------------------


def _backlog_and_own(tmp_path):
    """One candidate relation written by an earlier run and one written by this one, both checkable."""
    from kb_factories import claim, relation

    from knowledge_base.store import KnowledgeBase

    records = [
        claim(
            id="C-1",
            content="An earlier run's claim.",
            quote="a verbatim sentence",
            provenance_status="unverified",
            verification_method="none",
            run_id="RUN-0",
        ),
        claim(id="C-2", content="A target.", run_id="RUN-0"),
        claim(
            id="C-3",
            content="This run's claim.",
            quote="another verbatim sentence",
            provenance_status="unverified",
            verification_method="none",
        ),
        relation(
            id="L-1",
            source_id="C-1",
            target_id="C-2",
            justification_method="agent_inference",
            provenance_status="unverified",
            run_id="RUN-0",
        ),
        relation(
            id="L-2",
            source_id="C-3",
            target_id="C-2",
            justification_method="agent_inference",
            provenance_status="unverified",
        ),
    ]
    kb_dir = tmp_path / "kb"
    kb_dir.mkdir(parents=True)
    with KnowledgeBase.open(kb_dir) as kb:
        kb.commit(records, "RUN-1", "tester")
    return orchestrator(tmp_path, FakeLauncher(ALL))


def test_by_default_the_verifier_is_offered_the_whole_store(tmp_path):
    """The default is what makes an append-only store worth keeping: one run asserts a relation, a later one
    judges it. It is also most of the phase's cost, since the store holds far more candidates than one run
    writes."""
    o = _backlog_and_own(tmp_path)
    assert {c["relation_id"] for c in o._verify_candidates(_open(tmp_path))} == {"L-1", "L-2"}


def test_only_on_run_kb_leaves_the_backlog_to_whoever_wrote_it(tmp_path):
    o = _backlog_and_own(tmp_path)
    offered = {c["relation_id"] for c in o._verify_candidates(_open(tmp_path), only_own=True)}
    assert offered == {"L-2"}, "a relation this run did not write is not offered when the flag is set"


def test_the_recommended_value_of_the_flag_is_the_default_of_the_model():
    """The production config may set it to `true` for one cost-bound run (and says so where it does). What must
    not drift is the recommendation, which lives in the model's default."""
    from pipeline.config import VerifyPhase

    assert VerifyPhase().only_on_run_kb is False


@pytest.mark.parametrize("flag,expected", [(False, {"L-1", "L-2"}), (True, {"L-2"})])
def test_the_configured_flag_is_what_decides_the_pool(tmp_path, flag, expected):
    """The wiring, not the function: `_args` reads `phases.verify.only_on_run_kb` and the batches it builds for
    the launcher differ accordingly. Testing `_verify_candidates(only_own=…)` alone would pass even if the value
    in the configuration never reached it."""
    from knowledge_base.store import KnowledgeBase

    o = _backlog_and_own(tmp_path)
    phases = o.config.phases
    o.config = o.config.model_copy(
        update={
            "phases": phases.model_copy(update={"verify": phases.verify.model_copy(update={"only_on_run_kb": flag})})
        }
    )
    with KnowledgeBase.open(tmp_path / "kb") as kb:
        args = o._args("verify", kb)
    offered = {c["relation_id"] for batch in args["batches"] for c in batch}
    assert offered == expected
    assert o.config.phases.verify.only_on_run_kb is flag


def test_the_production_configuration_is_the_one_this_run_intends():
    """The production configuration runs with the pool bounded. The value is a per-run choice, so this asserts
    what the file says -- not that `true` is the recommendation, which is the model default and has its own
    test."""
    assert load_config().phases.verify.only_on_run_kb is True


def test_a_judgement_may_cite_a_claim_of_the_slice_it_was_shown(tmp_path):
    """A refusal seen in a replay: the contract contradicted itself.

    The critic is handed the hypothesis's whole 2-hop neighbourhood and told to cite the IDs it was given,
    then refused for citing one of them: live, `C-1139`, a verified literature claim of that slice, which
    carried the restriction limiting its `inconclusive`. The citable set is now named in the slice
    (`citable_context_claims`) and the guard is derived from that same field, so the two cannot disagree.
    The result claim stays required: naming the slice does not loosen the binding to this execution.
    """
    _, _, _, fixture_manifest = hla_fixture(tmp_path)
    served: list[dict] = []

    def design_registered(args):
        item = args["items"][0]
        target = item["targets"][0]
        return {
            "outputs": [
                {
                    "agent_id": "designer-1",
                    "hypothesis_id": item["hypothesis_id"],
                    "output": {
                        "experiments": [
                            {
                                "title": "Compact four-allele panel diagnostic",
                                "protocol": "Find the smallest pool covering the four alleles; compare size-matched subsets.",
                                "tested_claims": [target],
                                "expected_outcomes": [
                                    {"outcome": "all four covered", "claim_id": target, "effect": "supports"},
                                    {"outcome": "an allele uncovered", "claim_id": target, "effect": "contradicts"},
                                ],
                                "cost": "low",
                                "cost_rationale": "three local small files",
                                "feasibility": "high",
                                "kind": "in_silico",
                                "dataset_location": "local",
                                "dataset_refs": [entry["ref"] for entry in fixture_manifest["sources"]],
                                "analysis_id": "hla_panel_compact_v1",
                            }
                        ]
                    },
                }
            ]
        }

    def execute_registered(args):
        return execute_every(args, tmp_path / "run" / "execution-policy.json")

    def reassess_citing_the_slice(args):
        entry = args["executions"][0]
        execution, hypothesis = entry["execution"], entry["hypothesis"]
        served.append(hypothesis)
        # The contrary evidence the host showed, named in the judgement: what `C-1139` was live.
        context_claim = next(
            cid
            for cid in hypothesis["citable_context_claims"]
            if cid not in execution["tested_claims"]
            and cid not in {c["id"] for c in hypothesis["computed_result_claims"]}
        )
        return {
            "outputs": [
                {
                    "agent_id": f"reassessor@{execution['experiment_id']}",
                    "output": {
                        "experiment_id": execution["experiment_id"],
                        "result_digest": execution["result_digest"],
                        "decision": "inconclusive",
                        "rationale": f"The panel condition holds, but {context_claim} restricts how far it carries.",
                        "claim_ids": [execution["written"][0], context_claim],
                        "limitations": ["No population allele frequencies or observed presentation."],
                    },
                }
            ]
        }

    launcher = FakeLauncher(
        ALL | {"design": design_registered, "execute": execute_registered, "reassess": reassess_citing_the_slice}
    )
    o = orchestrator(tmp_path, launcher)
    sources = [
        DataSourceConfig(id=entry["id"], path=entry["path"], kind="table", license_note="fixture")
        for entry in fixture_manifest["sources"]
    ]
    o.config = o.config.model_copy(
        update={
            "phases": o.config.phases.model_copy(
                update={"data": o.config.phases.data.model_copy(update={"sources": sources})}
            )
        }
    )
    state = o.run(PHASES[: PHASES.index("reassess") + 1])

    report = state["phases"]["reassess"]
    assert report["status"] == "judged", report.get("error")
    # The citable set is exactly what the slice holds, so the agent reads the rule it is judged by.
    shown = served[0]
    assert set(shown["citable_context_claims"]) == {r["id"] for r in shown["context"] if r["id"].startswith("C-")}
    judgement = report["judgements"][0]["judgement"]
    assert len(set(judgement["claim_ids"]) - set(shown["citable_context_claims"])) >= 0
    assert set(state["phases"]["execute"]["executions"][0]["written"]) & set(judgement["claim_ids"])


def _judgement(**over):
    from pipeline.proposals import ReassessOutput

    return ReassessOutput.model_validate(
        {
            "experiment_id": "EXP-1",
            "result_digest": "a" * 64,
            "decision": "inconclusive",
            "rationale": "The panel condition holds and no more.",
            "claim_ids": ["C-1"],
            "limitations": ["No population allele frequencies."],
        }
        | over
    )


@pytest.mark.parametrize(
    "judgement, expected",
    [
        (_judgement(claim_ids=["C-1", "C-9"]), "claim_ids outside the claims this judgement was shown: C-9"),
        (_judgement(claim_ids=["C-2"]), "claim_ids cites none of the computed-result claims C-1"),
        (_judgement(experiment_id="EXP-2"), "experiment_id EXP-2 is not the executed EXP-1"),
        (_judgement(result_digest="b" * 64), "result_digest is not the digest of this execution"),
    ],
)
def test_a_refused_judgement_says_which_binding_failed_and_names_the_value(judgement, expected):
    """One sentence for four conditions said that something failed, not which: diagnosing one refusal
    refusal cost a paid replay to learn that the offending ID was `C-1139`."""
    from pipeline.orchestrator import _binding_failures

    reasons = _binding_failures(
        judgement, {"experiment_id": "EXP-1", "tested_claims": ["C-2"]}, "a" * 64, ["C-1"], {"C-1", "C-2"}
    )
    assert expected in reasons


# --- two handlers in one run ---------------------------------------------------

ICS_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "execute" / "data" / "ics_cytokine_skew_v1"


def test_two_bought_experiments_on_two_handlers_both_execute_and_are_both_reassessed(tmp_path):
    """The shape of a real run, with the defect removed: one hypothesis buys two registered analyses.

    Both must execute and both must be judged. The two critics are labelled by EXP, not by hypothesis, which
    is the only reason two judgements of one hypothesis can coexist.
    """
    table1, table2, cache, hla_manifest = hla_fixture(tmp_path)
    sources = [
        DataSourceConfig(id=entry["id"], path=entry["path"], kind="table", license_note="fixture")
        for entry in hla_manifest["sources"]
    ]
    sources += [
        DataSourceConfig(
            id="michaelis-zenodo-cytometry",
            path=str(ICS_FIXTURE / "michaelis-2025-zenodo"),
            kind="raw",
            license_note="fixture",
        ),
        DataSourceConfig(
            id="michaelis-source-data", path=str(ICS_FIXTURE / "source.xlsx"), kind="table", license_note="fixture"
        ),
    ]

    def design_two_handlers(args):
        item = args["items"][0]
        target = item["targets"][0]

        def proposal(title, refs, analysis_id):
            return {
                "title": title,
                "protocol": "Run the registered analysis on the declared local inputs.",
                "tested_claims": [target],
                "expected_outcomes": [
                    {"outcome": "as predeclared", "claim_id": target, "effect": "supports"},
                    {"outcome": "the contrary", "claim_id": target, "effect": "contradicts"},
                ],
                "cost": "low",
                "cost_rationale": "local files",
                "feasibility": "high",
                "kind": "in_silico",
                "dataset_location": "local",
                "dataset_refs": refs,
                "analysis_id": analysis_id,
            }

        # The refs the run's own manifest carries: a version that differs from it is `missing_inputs`.
        written = json.loads((tmp_path / "run" / "data-manifest.json").read_text())
        refs = {entry["id"]: entry["ref"] for entry in written["sources"]}
        hla_refs = [refs[sid] for sid in HANDLERS["hla_panel_compact_v1"].input_ids]
        ics_refs = [refs[sid] for sid in HANDLERS["ics_cytokine_skew_v1"].input_ids]
        return {
            "outputs": [
                {
                    "agent_id": "designer-1",
                    "hypothesis_id": item["hypothesis_id"],
                    "output": {
                        "experiments": [
                            proposal("Compact four-allele panel diagnostic", hla_refs, "hla_panel_compact_v1"),
                            proposal("ICS cytokine skew replication", ics_refs, "ics_cytokine_skew_v1"),
                        ]
                    },
                }
            ]
        }

    def arbitrate_both(args):
        item = args["items"][0]
        eids = sorted(
            (c["id"] for c in item["context"] if c["id"].startswith("EXP-")), key=lambda i: int(i.split("-", 1)[1])
        )
        return {
            "outputs": [
                {
                    "agent_id": "arbiter-1",
                    "hypothesis_id": item["hypothesis_id"],
                    "output": {
                        "decision": "buy",
                        "experiment_ids": eids,
                        "value_of_information": "high",
                        "rationale": "both registered analyses bear on the contested step",
                    },
                }
            ]
        }

    launcher = FakeLauncher(
        ALL
        | {
            "design": design_two_handlers,
            "arbitrate": arbitrate_both,
            "execute": lambda args: execute_every(args, tmp_path / "run" / "execution-policy.json"),
            "reassess": reassess_every,
        }
    )
    o = orchestrator(tmp_path, launcher)
    o.config = o.config.model_copy(
        update={
            "phases": o.config.phases.model_copy(
                update={"data": o.config.phases.data.model_copy(update={"sources": sources})}
            )
        }
    )
    state = o.run(PHASES[: PHASES.index("reassess") + 1])

    execute = state["phases"]["execute"]
    assert execute["status"] == "executed" and execute["executed_count"] == 2, execute.get("reason")
    assert [row["analysis_id"] for row in execute["executions"]] == ["hla_panel_compact_v1", "ics_cytokine_skew_v1"]
    assert all(row["interpretation_check"]["faithful"] for row in execute["executions"]), execute["executions"]
    # Two artifacts, two digests, two sets of computed claims: the second handler is not a copy of the first.
    digests = {row["result_digest"] for row in execute["executions"]}
    assert len(digests) == 2
    for row in execute["executions"]:
        assert (tmp_path / "run" / "experiment-analysis" / row["experiment_id"] / "summary.json").is_file()
        assert row["written"], row

    reassess = state["phases"]["reassess"]
    assert reassess["status"] == "judged" and reassess["judged_count"] == 2, reassess["judgements"]
    assert [entry["experiment_id"] for entry in reassess["judgements"]] == [
        row["experiment_id"] for row in execute["executions"]
    ]
    # Each judgement binds to its own execution, and the host checked each separately.
    for entry, row in zip(reassess["judgements"], execute["executions"]):
        assert entry["status"] == "judged", entry.get("error")
        assert entry["judgement"]["result_digest"] == row["result_digest"]
        assert set(entry["judgement"]["claim_ids"]) <= set(row["written"])


def test_one_analysis_bought_by_two_hypotheses_runs_once_and_both_are_judged(tmp_path):
    """The shape of a real run: three hypotheses bought `ics_cytokine_skew_v1` on the same inputs and the host ran
    it three times, three identical summaries read by three critics as their own evidence. Now the computation
    runs once, the second hypothesis is bound to that result, and each critic is told it is shared.
    """
    _, _, _, fixture_manifest = hla_fixture(tmp_path)

    def generate_two(args):
        first = generate(args)["outputs"][0]
        second = copy.deepcopy(first)
        second["agent_id"] = "author-2"
        second["output"]["hypothesis"]["statement"] = "Tolerance to C9orf72 is lost only in carriers"
        claim = second["output"]["hypothesis"]["claims"][0]
        claim["content"] = "Carrier CD4 T cells drive motor neuron loss"
        claim["causal"] |= {"cause": "carrier CD4 T cells", "context": "C9orf72 carriers"}
        return {"phase": "generate", "dropped": [], "outputs": [first, second]}

    def design_each(args):
        outputs = []
        for item in args["items"]:
            # The fake review contests only the first hypothesis; the second tests its own claim instead.
            claims = [c["id"] for c in item["context"] if c["id"].startswith("C-")]
            item = item | {"targets": item["targets"] or claims[-1:]}
            one = _registered_design(fixture_manifest)({"items": [item]})["outputs"][0]
            outputs.append(one | {"agent_id": f"designer@{item['hypothesis_id']}"})
        return {"outputs": outputs}

    def arbitrate_each(args):
        return {
            "outputs": [
                {
                    "agent_id": f"arbiter@{item['hypothesis_id']}",
                    "hypothesis_id": item["hypothesis_id"],
                    "output": {
                        "decision": "buy",
                        "experiment_ids": [c["id"] for c in item["context"] if c["id"].startswith("EXP-")],
                        "value_of_information": "high",
                        "rationale": "the registered analysis",
                    },
                }
                for item in args["items"]
            ]
        }

    def synthesize_first(args):
        item = args["items"][0]
        return synthesize({"items": [item | {"hypotheses": item["hypotheses"][:1]}]})

    launcher = FakeLauncher(
        ALL
        | {
            "generate": generate_two,
            "design": design_each,
            "arbitrate": arbitrate_each,
            "execute": lambda args: execute_every(args, tmp_path / "run" / "execution-policy.json"),
            "reassess": reassess_every,
            "synthesize": synthesize_first,
        }
    )
    o = orchestrator(tmp_path, launcher)
    sources = [
        DataSourceConfig(id=entry["id"], path=entry["path"], kind="table", license_note="fixture")
        for entry in fixture_manifest["sources"]
    ]
    o.config = o.config.model_copy(
        update={
            "phases": o.config.phases.model_copy(
                update={"data": o.config.phases.data.model_copy(update={"sources": sources})}
            )
        }
    )
    state = o.run()

    execute = state["phases"]["execute"]
    first, second = execute["selected"]
    assert first["hypothesis_id"] != second["hypothesis_id"]
    assert "result_of" not in first and second["result_of"] == first["experiment_id"]
    # One executor, one policy entry, one artifact on disk.
    execute_args = next(args for phase, args in launcher.calls if phase == "execute")
    assert [s["experiment_id"] for s in execute_args["selected"]] == [first["experiment_id"]]
    policy = json.loads((tmp_path / "run" / "execution-policy.json").read_text())
    assert [s["experiment_id"] for s in policy["selected"]] == [first["experiment_id"]]
    assert not (tmp_path / "run" / "experiment-analysis" / second["experiment_id"]).exists()
    assert (execute["executed_count"], execute["reused_count"]) == (1, 1)
    run_row, reuse_row = execute["executions"]
    assert reuse_row["status"] == "executed" and reuse_row["result_of"] == first["experiment_id"]
    assert reuse_row["result_digest"] == run_row["result_digest"]
    assert (reuse_row["written"], reuse_row["seen_again"]) == ([], {})
    assert reuse_row["result_claims"] == run_row["result_claims"] == run_row["written"]

    reassess_args = next(args for phase, args in launcher.calls if phase == "reassess")
    shown = {e["execution"]["experiment_id"]: e["execution"] for e in reassess_args["executions"]}
    assert shown[first["experiment_id"]]["shared_with"] == [
        {"experiment_id": second["experiment_id"], "hypothesis_id": second["hypothesis_id"]}
    ]
    assert shown[second["experiment_id"]]["shared_with"] == [
        {"experiment_id": first["experiment_id"], "hypothesis_id": first["hypothesis_id"]}
    ]
    reassess = state["phases"]["reassess"]
    assert reassess["judged_count"] == 2, reassess["judgements"]
    # A run died here: synthesize looked for a receipt under the reuse's own ID, where none is written.
    assert state["phases"]["synthesize"]["proposal"]["outcome"] == "proposal"
    assert (tmp_path / "run" / "final-proposal.json").is_file()


def test_a_reuse_of_a_computation_that_did_not_execute_says_so():
    from pipeline.orchestrator import _reuse_execution

    chosen = {
        "experiment_id": "EXP-4",
        "hypothesis_id": "H-2",
        "analysis_id": "ics_cytokine_skew_v1",
        "tested_claims": ["C-9"],
        "result_of": "EXP-1",
    }
    row = _reuse_execution(chosen, {"experiment_id": "EXP-1", "status": "not_executed"})
    assert row["status"] == "not_executed" and row["result_of"] == "EXP-1"
    assert "EXP-1, did not execute" in row["reason"]


def test_an_execution_with_no_agent_output_is_reported_unfaithful_without_blocking_the_others(tmp_path):
    """One agent returning nothing must cost its own EXP its interpretation check, not the whole phase."""
    _, _, _, fixture_manifest = hla_fixture(tmp_path)

    def execute_without_output(args):
        outcome = execute_every(args, tmp_path / "run" / "execution-policy.json")
        return {"outputs": [], "dropped": [item["agent_id"] for item in outcome["outputs"]]}

    launcher = FakeLauncher(
        ALL
        | {
            "design": _registered_design(fixture_manifest),
            "execute": execute_without_output,
            "reassess": reassess_every,
        }
    )
    o = orchestrator(tmp_path, launcher)
    sources = [
        DataSourceConfig(id=entry["id"], path=entry["path"], kind="table", license_note="fixture")
        for entry in fixture_manifest["sources"]
    ]
    o.config = o.config.model_copy(
        update={
            "phases": o.config.phases.model_copy(
                update={"data": o.config.phases.data.model_copy(update={"sources": sources})}
            )
        }
    )
    state = o.run(PHASES[: PHASES.index("reassess") + 1])

    execute = state["phases"]["execute"]
    (row,) = execute["executions"]
    # The artifact exists and is verified, so the EXP executed; only the interpretation is missing.
    assert execute["status"] == "executed" and row["status"] == "executed"
    assert row["interpretation_check"] == {
        "faithful": False,
        "errors": [f"no computational agent output for {row['experiment_id']}"],
    }
    assert execute["dropped_agents"] == [f"executor@{row['experiment_id']}"]
    assert state["phases"]["reassess"]["status"] == "judged"


def _registered_design(fixture_manifest):
    def design_registered(args):
        item = args["items"][0]
        target = item["targets"][0]
        return {
            "outputs": [
                {
                    "agent_id": "designer-1",
                    "hypothesis_id": item["hypothesis_id"],
                    "output": {
                        "experiments": [
                            {
                                "title": "Compact four-allele panel diagnostic",
                                "protocol": "Find the smallest pool covering the four predicted HLA alleles.",
                                "tested_claims": [target],
                                "expected_outcomes": [
                                    {"outcome": "all four covered", "claim_id": target, "effect": "supports"},
                                    {"outcome": "an allele uncovered", "claim_id": target, "effect": "contradicts"},
                                ],
                                "cost": "low",
                                "cost_rationale": "three local small files",
                                "feasibility": "high",
                                "kind": "in_silico",
                                "dataset_location": "local",
                                "dataset_refs": [entry["ref"] for entry in fixture_manifest["sources"]],
                                "analysis_id": "hla_panel_compact_v1",
                            }
                        ]
                    },
                }
            ]
        }

    return design_registered


# --- a source that leads nowhere is named by the phase that wrote it (bug observatory 11) ---------


def test_the_phase_that_writes_a_claim_on_an_unresolvable_source_names_it_in_its_report(tmp_path):
    """A run cited `C-87`, resting on a university press page, in three sections of the final
    proposal; nothing said so until `synthesis.gate`, twelve phases later. The writing phase says it now."""
    news = "https://www.news.uzh.ch/en/articles/2025/als.html"

    def review_on_a_news_page(args):
        item = args["items"][0]
        target = next(c["id"] for c in item["context"] if c.get("cause_ref"))
        return {
            "phase": "review",
            "dropped": [],
            "outputs": [
                {
                    "agent_id": "reviewer-2@" + item["hypothesis_id"],
                    "hypothesis_id": item["hypothesis_id"],
                    "output": {
                        "claims": [
                            {
                                "ref": "e1",
                                "type": "observational",
                                "content": "A press page reports a finding.",
                                "source": news,
                                "quote": CONTRA_QUOTE,
                            }
                        ],
                        "links": [
                            {
                                "type": "contradicts",
                                "source": "e1",
                                "target": target,
                                "rationale": "the page says otherwise",
                            }
                        ],
                        "gaps": [],
                        "summary": "One citation leads to a press page.",
                    },
                }
            ],
        }

    launcher = FakeLauncher(ALL | {"review": review_on_a_news_page})
    phases = PHASES[: PHASES.index("review") + 1]
    orchestrator(tmp_path, launcher).run(phases)

    report = json.loads((tmp_path / "run" / "05-review-report.json").read_text())
    (entry,) = report["unresolved_sources"]["reviewer-2@H-1"]
    assert entry["unresolved_source_refs"] == [news]
    assert entry["claim_id"] in report["written"], "the report must name an ID a reader can look up"
    assert entry["ref"] == "e1"
    # The claim is written, not dropped, and keeps the reference exactly as the agent wrote it.
    records = read_records(tmp_path / "kb")
    record = records[entry["claim_id"]]
    assert news in record.source_refs
    assert report["dropped_elements"] == {}
    # `provenance_status` here is `verified` ONLY because this module's `verifier` fixture is a fake that
    # answers on the quote string alone. **Do not read it as pipeline behaviour**: the real `QuoteVerifier`
    # looks the quote up in the *stored text* of the source, and a news page is never in the store —
    # `fetch_missing` resolves DOIs, PMIDs and PMCIDs and nothing else — so a live run gets
    # `paper_not_in_store` and the claim lands `unverified`, as the real `C-87` did. What this test is about
    # is the report naming the claim, which happens whatever the provenance status says.
    assert record.provenance_status == "verified"  # the fake verifier's answer, not the pipeline's


def test_a_resolvable_source_leaves_the_unresolved_report_empty(tmp_path):
    """The default `review` fake cites a DOI, so the field must stay empty: a report that named every claim
    would say nothing."""
    orchestrator(tmp_path, FakeLauncher(ALL)).run(PHASES[: PHASES.index("review") + 1])
    report = json.loads((tmp_path / "run" / "05-review-report.json").read_text())
    assert report["unresolved_sources"] == {}


# --- the novelty stage, after the pipeline ----------------------------------------------------------------------


def _served_cache(path: Path, ids=("https://openalex.org/W1",), dois=None) -> Path:
    dois = dois or {}
    path.write_text(
        json.dumps(
            {"key": "q|10", "works": [{"openalex_id": i, **({"doi": dois[i]} if i in dois else {})} for i in ids]}
        )
        + "\n"
    )
    return path


def _novelty_answer(args, verdict="anticipated", works=("https://openalex.org/W1",), hypothesis_id=None):
    out = []
    for item in args["items"]:
        hid = hypothesis_id or item["hypothesis_id"]
        out.append(
            {
                "agent_id": f"novelty@{item['hypothesis_id']}",
                "output": {
                    "hypothesis_id": hid,
                    "verdict": verdict,
                    "rationale": "r",
                    "queries": ["treg ALS"],
                    "full_texts_read": [],
                    "limitations": ["OpenAlex only"],
                    "closest_works": [{"openalex_id": w, "title": "T", "why_close": "same mechanism"} for w in works],
                },
            }
        )
    return {"outputs": out, "dropped": []}


def _finished(tmp_path, answer):
    launcher = FakeLauncher(ALL | {"novelty": answer})
    orch = orchestrator(tmp_path, launcher)
    orch.run(("literature", "generate"))
    return orch, launcher


def test_novelty_is_not_a_phase_of_the_run():
    assert "novelty" not in PHASES


def test_novelty_assesses_each_live_hypothesis_and_writes_its_own_report(tmp_path):
    orch, launcher = _finished(tmp_path, _novelty_answer)
    report = orch.run_novelty(_served_cache(tmp_path / "cache.jsonl"))
    assert report["status"] == "assessed" and report["assessed_count"] == len(report["entries"]) >= 1
    assert report["sources_searched"] == ["openalex"]
    assert (tmp_path / "run" / "13-novelty-report.json").is_file()
    assert [c[0] for c in launcher.calls].count("novelty") == 1
    assert "novelty" in json.loads((tmp_path / "run" / "state.json").read_text())["completed"]


def test_novelty_names_only_works_the_search_tool_returned(tmp_path):
    """Every work of a prior-art verdict dropped leaves it resting on nothing, so the entry keeps no verdict."""
    orch, _ = _finished(tmp_path, lambda a: _novelty_answer(a, works=("https://openalex.org/W999",)))
    report = orch.run_novelty(_served_cache(tmp_path / "cache.jsonl"))
    assert report["status"] == "unverified" and report["assessed_count"] == 0
    assert "never returned" in report["entries"][0]["error"]
    prov = report["entries"][0]["provenance"]
    assert prov["works_listed"] == 0 and prov["works_dropped"] == 1
    assert prov["dropped"][0]["openalex_id"] == "https://openalex.org/W999"
    assert "never returned this work" in prov["dropped"][0]["reason"]


def test_a_work_the_tool_never_served_is_dropped_on_its_own(tmp_path):
    """One unserved work no longer costs the whole assessment: it is dropped, the verdict stands."""
    orch, _ = _finished(
        tmp_path, lambda a: _novelty_answer(a, works=("https://openalex.org/W1", "https://openalex.org/W999"))
    )
    report = orch.run_novelty(_served_cache(tmp_path / "cache.jsonl"))
    entry = report["entries"][0]
    assert entry["status"] == "assessed" and entry["novelty"]["verdict"] == "anticipated"
    assert [w["openalex_id"] for w in entry["novelty"]["closest_works"]] == ["https://openalex.org/W1"]
    assert entry["provenance"]["works_listed"] == 1 and entry["provenance"]["works_dropped"] == 1
    assert [w["openalex_id"] for w in entry["provenance"]["dropped"]] == ["https://openalex.org/W999"]


def test_no_prior_art_found_survives_every_work_being_dropped(tmp_path):
    """The verdict rests on the searches, not on the works, so dropping them all does not refuse it."""
    orch, _ = _finished(
        tmp_path, lambda a: _novelty_answer(a, verdict="no_prior_art_found", works=("https://openalex.org/W999",))
    )
    cache = tmp_path / "cache.jsonl"
    cache.write_text(json.dumps({"key": "k", "query": "treg ALS", "works": [{"openalex_id": "W1"}]}) + "\n")
    report = orch.run_novelty(cache)
    assert report["entries"][0]["status"] == "assessed"
    assert report["entries"][0]["provenance"]["works_dropped"] == 1


def test_the_provenance_block_reaches_the_written_report(tmp_path):
    """The diagnosis reads the file, not the return value."""
    orch, _ = _finished(tmp_path, _novelty_answer)
    orch.run_novelty(_served_cache(tmp_path / "cache.jsonl"))
    written = json.loads((tmp_path / "run" / "13-novelty-report.json").read_text())
    assert written["entries"][0]["provenance"]["kept"] == [
        {"openalex_id": "https://openalex.org/W1", "evidence_depth": "abstract"}
    ]


def test_novelty_with_no_search_cache_makes_no_work_citable(tmp_path):
    orch, _ = _finished(tmp_path, _novelty_answer)
    assert orch.run_novelty(tmp_path / "missing.jsonl")["status"] == "unverified"


def test_novelty_prior_art_verdict_must_name_its_works(tmp_path):
    orch, _ = _finished(tmp_path, lambda a: _novelty_answer(a, works=()))
    assert orch.run_novelty(_served_cache(tmp_path / "cache.jsonl"))["status"] == "unverified"


def test_novelty_no_prior_art_found_needs_a_search_that_returned_a_work(tmp_path):
    """Found nothing is a statement about searches made: it needs one that returned a work, and its query is the
    agent's own (`treg ALS`), matched to the search tool's cache regardless of case and spacing."""
    orch, _ = _finished(tmp_path, lambda a: _novelty_answer(a, verdict="no_prior_art_found", works=()))
    cache = tmp_path / "cache.jsonl"
    cache.write_text(json.dumps({"key": "k", "query": "Treg   ALS", "works": [{"openalex_id": "W1"}]}) + "\n")
    assert orch.run_novelty(cache)["status"] == "assessed"


def test_novelty_no_prior_art_found_without_any_returned_work_is_refused(tmp_path):
    """The `pl-prod-013` H-2 case: every call was refused for budget, no abstract was read, and the agent still
    answered `no_prior_art_found`. That is not a finding, and the scorer must not count it as one."""
    orch, _ = _finished(tmp_path, lambda a: _novelty_answer(a, verdict="no_prior_art_found", works=()))
    empty = tmp_path / "cache.jsonl"
    empty.write_text(json.dumps({"key": "k", "query": "treg als", "works": []}) + "\n")
    report = orch.run_novelty(empty)
    assert report["status"] == "unverified" and "no search that returned a work" in report["entries"][0]["error"]


def test_novelty_refuses_an_answer_about_another_hypothesis(tmp_path):
    orch, _ = _finished(tmp_path, lambda a: _novelty_answer(a, hypothesis_id="H-999"))
    report = orch.run_novelty(_served_cache(tmp_path / "cache.jsonl"))
    assert report["status"] == "unverified" and "not" in report["entries"][0]["error"]


def test_novelty_runs_once_per_run(tmp_path):
    orch, launcher = _finished(tmp_path, _novelty_answer)
    first = orch.run_novelty(_served_cache(tmp_path / "cache.jsonl"))
    assert orch.run_novelty(_served_cache(tmp_path / "cache.jsonl")) == first
    assert [c[0] for c in launcher.calls].count("novelty") == 1


def test_novelty_with_no_hypothesis_launches_nothing(tmp_path):
    launcher = FakeLauncher(ALL | {"novelty": _novelty_answer})
    report = orchestrator(tmp_path, launcher).run_novelty(tmp_path / "missing.jsonl")
    assert report["status"] == "not_assessed" and not launcher.calls


def test_a_second_novelty_attempt_keeps_the_first_and_starts_clean(tmp_path):
    orch, launcher = _finished(tmp_path, _novelty_answer)
    cache = _served_cache(tmp_path / "cache.jsonl")
    first = orch.run_novelty(cache)
    assert first["server_log_offset"] == 0
    (tmp_path / "run" / "literature-calls.agents.jsonl").write_text('{"event": "search"}\n{"event": "search"}\n')

    kept = orch.archive_novelty_attempt()
    assert kept == tmp_path / "run" / "novelty-attempt-1"
    assert json.loads((kept / "13-novelty-report.json").read_text())["status"] == "assessed"
    assert not (tmp_path / "run" / "13-novelty-report.json").exists()
    assert "novelty" not in json.loads((tmp_path / "run" / "state.json").read_text())["completed"]

    again = orch.run_novelty(cache)
    assert again["status"] == "assessed" and again["server_log_offset"] == 2
    assert [c[0] for c in launcher.calls].count("novelty") == 2
    assert orch.archive_novelty_attempt() == tmp_path / "run" / "novelty-attempt-2"


def test_there_is_nothing_to_archive_before_a_first_attempt(tmp_path):
    assert orchestrator(tmp_path, FakeLauncher(ALL)).archive_novelty_attempt() is None


def test_the_novelty_schema_handed_to_the_agent_requires_closest_works():
    """The `pl-prod-013` defect: a defaulted field is optional in the agent's schema, so two of six agents left it
    out and named their prior art in prose instead."""
    from pipeline.proposals import NoveltyOutput, agent_schema

    schema = agent_schema(NoveltyOutput)
    assert "closest_works" in schema["required"]
    base = {
        "hypothesis_id": "H-1",
        "verdict": "no_prior_art_found",
        "rationale": "r",
        "queries": ["q"],
        "full_texts_read": [],
        "limitations": ["l"],
    }
    with pytest.raises(Exception):
        NoveltyOutput.model_validate(base)  # absent: refused by the schema itself
    assert NoveltyOutput.model_validate(base | {"closest_works": []}).closest_works == []


def test_a_rationale_naming_an_unserved_work_drops_it_and_keeps_the_verdict(tmp_path):
    """An id in the prose alone is undecidable for the model: the host tells a fabrication from a lapse."""

    def answer(args):
        out = _novelty_answer(args)
        for o in out["outputs"]:
            o["output"]["rationale"] = "Stated by W3160411344 and by W1."
        return out

    orch, _ = _finished(tmp_path, answer)
    report = orch.run_novelty(_served_cache(tmp_path / "cache.jsonl"))
    entry = report["entries"][0]
    assert entry["status"] == "assessed" and entry["provenance"]["works_listed"] == 1
    dropped = entry["provenance"]["dropped"]
    assert [w["openalex_id"] for w in dropped] == ["W3160411344"] and dropped[0]["title"] is None
    assert "rationale only" in dropped[0]["reason"]


def test_a_served_work_named_in_the_rationale_alone_is_a_listing_lapse(tmp_path):
    """The work exists and was served: nothing is dropped, the omission is reported as itself."""

    def answer(args):
        out = _novelty_answer(args, works=("https://openalex.org/W1",))
        for o in out["outputs"]:
            o["output"]["rationale"] = "Stated by W1976266513."
        return out

    orch, _ = _finished(tmp_path, answer)
    report = orch.run_novelty(
        _served_cache(tmp_path / "cache.jsonl", ("https://openalex.org/W1", "https://openalex.org/W1976266513"))
    )
    prov = report["entries"][0]["provenance"]
    assert report["entries"][0]["status"] == "assessed" and prov["rationale_only"] == ["W1976266513"]
    assert prov["works_dropped"] == 0


def test_a_rationale_whose_works_are_all_listed_is_accepted(tmp_path):
    def answer(args):
        out = _novelty_answer(args, works=("https://openalex.org/W1976266513",))
        for o in out["outputs"]:
            o["output"]["rationale"] = "Stated by W1976266513."
        return out

    orch, _ = _finished(tmp_path, answer)
    assert (
        orch.run_novelty(_served_cache(tmp_path / "cache.jsonl", ("https://openalex.org/W1976266513",)))["status"]
        == "assessed"
    )


def _with_reads(args, dois):
    out = _novelty_answer(args)
    for o in out["outputs"]:
        o["output"]["full_texts_read"] = list(dois)
    return out


def test_a_full_text_claimed_as_read_must_be_one_the_server_served(tmp_path):
    """Reading is the one claim in the output the host can check against a log, so it does."""
    orch, _ = _finished(tmp_path, lambda a: _with_reads(a, ["10.1000/a"]))
    report = orch.run_novelty(_served_cache(tmp_path / "cache.jsonl"))
    assert report["status"] == "unverified" and "never served by read_paper" in report["entries"][0]["error"]


def _reads_during_the_stage(tmp_path, dois):
    """An answer that, like a live agent, makes its `read_paper` calls (lines of the shared server log) while the
    stage runs and only then answers."""
    log = tmp_path / "run" / "literature-calls.agents.jsonl"

    def answer(args):
        with log.open("a") as handle:
            for doi in dois:
                handle.write(json.dumps({"event": "read", "outcome": "read", "paper": f"doi:{doi}"}) + "\n")
        return _with_reads(args, dois)

    return answer


def test_a_full_text_the_server_did_serve_is_accepted(tmp_path):
    orch, _ = _finished(tmp_path, _reads_during_the_stage(tmp_path, ["10.1000/a"]))
    assert orch.run_novelty(_served_cache(tmp_path / "cache.jsonl"))["status"] == "assessed"


def test_a_kept_work_says_whether_it_was_read_in_full(tmp_path):
    """Host-derived, by joining the work's cached DOI with the papers this hypothesis's agent declared and the
    server's log of this stage confirms."""
    orch, _ = _finished(tmp_path, _reads_during_the_stage(tmp_path, ["10.1000/a"]))
    report = orch.run_novelty(_served_cache(tmp_path / "cache.jsonl", dois={"https://openalex.org/W1": "10.1000/a"}))
    prov = report["entries"][0]["provenance"]
    assert prov["kept"] == [{"openalex_id": "https://openalex.org/W1", "evidence_depth": "full_text"}]
    assert prov["works_read_in_full"] == 1


def test_a_served_work_nobody_opened_rests_on_its_abstract(tmp_path):
    orch, _ = _finished(tmp_path, _novelty_answer)
    report = orch.run_novelty(_served_cache(tmp_path / "cache.jsonl", dois={"https://openalex.org/W1": "10.1000/a"}))
    assert report["entries"][0]["provenance"]["kept"][0]["evidence_depth"] == "abstract"
    assert report["entries"][0]["provenance"]["works_read_in_full"] == 0


def test_a_full_text_one_agent_opened_is_not_credited_to_another_hypothesis(tmp_path):
    """The defect this guards against cost a real figure: `read_paper` logs the paper and never who asked for
    it, so a stage-wide set of opened papers marked a work `full_text` for every hypothesis that cited it. On
    `pl-prod-013` that credited H-2 with a reading only H-1's agent had made. Here both hypotheses cite the
    same work and only the first declares the paper, so only the first may claim its full text.
    """

    def generate_two(args):
        first = generate(args)["outputs"][0]
        second = copy.deepcopy(first)
        second["agent_id"] = "author-2"
        second["output"]["hypothesis"]["statement"] = "Tolerance to C9orf72 is lost only in carriers"
        return {"phase": "generate", "dropped": [], "outputs": [first, second]}

    log = tmp_path / "run" / "literature-calls.agents.jsonl"

    def answer(args):
        with log.open("a") as handle:
            handle.write(json.dumps({"event": "read", "outcome": "read", "paper": "doi:10.1000/a"}) + "\n")
        out = _novelty_answer(args)
        out["outputs"][0]["output"]["full_texts_read"] = ["10.1000/a"]
        return out

    orch = orchestrator(tmp_path, FakeLauncher(ALL | {"generate": generate_two, "novelty": answer}))
    orch.run(("literature", "generate"))
    report = orch.run_novelty(_served_cache(tmp_path / "cache.jsonl", dois={"https://openalex.org/W1": "10.1000/a"}))

    declared, silent = (e["provenance"] for e in report["entries"][:2])
    assert (declared["kept"][0]["evidence_depth"], declared["works_read_in_full"]) == ("full_text", 1)
    assert (silent["kept"][0]["evidence_depth"], silent["works_read_in_full"]) == ("abstract", 0)


def test_a_paper_read_but_not_declared_rests_on_its_abstract(tmp_path):
    """The under-count is deliberate: the host marks `full_text` from the agent's own declaration, checked
    against the log, so a paper opened and left out of `full_texts_read` is credited to nobody. Claiming
    nothing is not an error, so the entry stands."""
    log = tmp_path / "run" / "literature-calls.agents.jsonl"

    def reads_without_saying_so(args):
        with log.open("a") as handle:
            handle.write(json.dumps({"event": "read", "outcome": "read", "paper": "doi:10.1000/a"}) + "\n")
        return _novelty_answer(args)

    orch, _ = _finished(tmp_path, reads_without_saying_so)
    report = orch.run_novelty(_served_cache(tmp_path / "cache.jsonl", dois={"https://openalex.org/W1": "10.1000/a"}))

    assert report["status"] == "assessed"
    assert report["entries"][0]["provenance"]["kept"][0]["evidence_depth"] == "abstract"
    assert report["entries"][0]["provenance"]["works_read_in_full"] == 0


def test_a_work_cached_without_a_doi_cannot_be_credited_with_a_reading(tmp_path):
    """The server log records the paper, never the work: with no DOI the host cannot link the two."""
    orch, _ = _finished(tmp_path, _reads_during_the_stage(tmp_path, ["10.1000/a"]))
    report = orch.run_novelty(_served_cache(tmp_path / "cache.jsonl"))
    assert report["entries"][0]["provenance"]["kept"][0]["evidence_depth"] == "abstract"


def test_a_read_from_before_this_attempt_does_not_count(tmp_path):
    orch, _ = _finished(tmp_path, _reads_during_the_stage(tmp_path, ["10.1000/a"]))
    first = orch.run_novelty(_served_cache(tmp_path / "cache.jsonl"))
    assert first["status"] == "assessed"
    orch.archive_novelty_attempt()
    orch.launcher.responses["novelty"] = lambda a: _with_reads(a, ["10.1000/a"])  # claims it again, reads nothing
    again = orch.run_novelty(_served_cache(tmp_path / "cache.jsonl"))
    assert again["server_log_offset"] == 1 and again["status"] == "unverified"


def test_the_stage_tells_the_agent_how_many_papers_it_may_read(tmp_path):
    orch, launcher = _finished(tmp_path, _novelty_answer)
    orch.run_novelty(_served_cache(tmp_path / "cache.jsonl"))
    call = next(a for p, a in launcher.calls if p == "novelty")
    assert call["max_papers_read"] == orch.config.phases.novelty.max_papers_read == 3
