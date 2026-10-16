"""Writes `kb/`: two hypotheses of one run whose bought EXPs name the same analysis on the same inputs.

Run once with `UV_PROJECT_ENVIRONMENT=.venv uv run --frozen python tests/fixtures/synthesize/build_kb.py` and commit
the output; it is not a test and pytest does not collect it. Re-run it only if the fixture's shape must change.

`select_all` runs the computation once, for EXP-1, and binds EXP-2 to it as a reuse (ADR 0030). That is the shape
that made `pl-prod-012` fail at synthesize. All content is synthetic and asserts nothing about ALS.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parents[1]), str(HERE.parents[2])]  # tests/ (kb_factories) and the repository root

from kb_factories import claim, decision, experiment, hypothesis, relation  # noqa: E402

from knowledge_base.store import KnowledgeBase  # noqa: E402

RUN = "FIX-SYNTH"
SOURCES = ["dataset:michaelis-zenodo-cytometry", "dataset:michaelis-source-data"]


def records() -> list:
    out = []
    for n in (1, 2):
        out += [
            hypothesis(
                id=f"H-{n}",
                run_id=RUN,
                agent_id="fixture",
                statement=f"Synthetic fixture hypothesis {n}: the CD4+ cytokine ratio differs in ALS donors.",
                mechanism=f"Synthetic mechanism {n}.",
            ),
            claim(
                id=f"C-{n}",
                type="assumption",
                origin="agent",
                provenance_status="unverified",
                verification_method="none",
                source_refs=[],
                run_id=RUN,
                content=f"Synthetic fixture claim {n}: the IFNg/(IL-4+IL-10) ratio is lower in ALS donors.",
            ),
            relation(
                id=f"L-{n}",
                source_id=f"C-{n}",
                target_id=f"H-{n}",
                type="part_of",
                justification_method="agent_inference",
                provenance_status="unverified",
                source_refs=[],
                run_id=RUN,
            ),
            experiment(
                id=f"EXP-{n}",
                hypothesis_id=f"H-{n}",
                tested_claims=[f"C-{n}"],
                run_id=RUN,
                title=f"ICS cytokine skew re-analysis for hypothesis {n}",
                protocol="Recompute each donor's CD4+ cytokine ratio from the deposited gates and compare groups.",
                expected_outcomes=[
                    {"outcome": "ALS ratio lower", "claim_id": f"C-{n}", "effect": "supports"},
                    {"outcome": "no difference", "claim_id": f"C-{n}", "effect": "contradicts"},
                ],
                cost="low",
                cost_rationale="two local files",
                feasibility="high",
                kind="in_silico",
                dataset_refs=SOURCES,
                dataset_location="local",
                required_markers=[],
                required_metadata=[],
                analysis_id="ics_cytokine_skew_v1",
                agent_id="fixture",
            ),
            decision(
                id=f"D-{n}",
                hypothesis_id=f"H-{n}",
                experiment_ids=[f"EXP-{n}"],
                run_id=RUN,
                rationale=f"EXP-{n} settles claim {n}.",
                agent_id="fixture",
            ),
        ]
    return out


if __name__ == "__main__":
    target = HERE / "kb"
    shutil.rmtree(target, ignore_errors=True)
    target.mkdir()
    with KnowledgeBase.open(target) as kb:
        result = kb.commit(records(), RUN, "fixture")
    assert not result.rejected, result.rejected
    (target / ".lock").unlink(missing_ok=True)
    print(sorted(p.name for p in target.iterdir()))
