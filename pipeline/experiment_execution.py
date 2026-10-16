"""Select the bought, data-supported registered EXPs of a run and check each agent interpretation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from analysis.experiment_runner import HANDLERS
from analysis.verify import verify_receipt
from knowledge_base.schemas import Decision, Experiment

from .experiment_feasibility import dataset_id
from .proposals import ExecuteOutput


def select_all(
    records: Mapping[str, Any], run_id: str, feasibility: Mapping[str, Mapping[str, Any]], max_experiments: int
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Every eligible bought EXP, in ID order. Every excluded bought EXP keeps a reason in the report.

    Previously this returned on the **first** survivor, so an earlier run executed `EXP-1` and never
    examined `EXP-2`, `EXP-4` or `EXP-5` — three bought, `data_supported` EXPs, two of them on the second
    registered handler, which has therefore never run in production. They were not even recorded as skipped
    (the report reads `"skipped": []`), because the loop stopped before reaching them. The arbiter decides
    which experiments to buy; dropping all but one of them, silently, was this function's defect and not a
    decision anyone made.

    A request is fully determined by its analysis and its input files: a handler takes nothing else and is
    deterministic. So that pair is a cache key, and a computation runs once per run whoever bought it:

    - a second EXP of the **same** hypothesis on the same key is skipped — it would add nothing;
    - an EXP of a **different** hypothesis on the same key is selected with `result_of` naming the EXP that
      computes it. It launches no executor and is bound to that result by the host; its hypothesis is still
      judged on it, because the hypothesis is new even when the result is not.
      an earlier run ran `ics_cytokine_skew_v1` three times on the same two inputs, for three hypotheses, and
      got three identical summaries that three reassessors each read as their own evidence.

    `max_experiments` is a cost bound, not a scientific rule, so an EXP excluded by it says so in its reason. It
    counts computations: a reuse costs none.
    """
    bought = sorted(
        {
            eid
            for record in records.values()
            if isinstance(record, Decision) and record.run_id == run_id and record.decision == "buy"
            for eid in record.experiment_ids
        },
        key=lambda eid: int(eid.split("-", 1)[1]),
    )
    selected: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    computed_by: dict[tuple[str, tuple[str, ...]], dict[str, Any]] = {}  # key -> the EXP computing it
    hypotheses_on: dict[tuple[str, tuple[str, ...]], set[str]] = {}  # key -> hypotheses already bound
    computations = 0
    for eid in bought:
        exp = records.get(eid)
        row = feasibility.get(eid, {})
        if not isinstance(exp, Experiment) or exp.run_id != run_id:
            reason = "EXP record is absent or belongs to another run"
        elif exp.analysis_id not in HANDLERS:
            reason = "no registered execution handler"
        elif exp.kind != "in_silico" or exp.dataset_location != "local":
            reason = "registered handler requires a local in_silico EXP"
        elif set(map(dataset_id, exp.dataset_refs)) != set(HANDLERS[exp.analysis_id].input_ids):
            reason = "EXP does not name exactly the handler's inputs"
        elif row.get("status") != "data_supported":
            reason = f"data feasibility is {row.get('status', 'unverified')}: {row.get('reason', 'no row')}"
        elif exp.hypothesis_id in hypotheses_on.get(
            key := _computation_key(exp.analysis_id, exp.dataset_refs, row), set()
        ):
            reason = "an EXP of this hypothesis already runs this analysis on these inputs"
        elif (runner := computed_by.get(key)) is None and computations >= max_experiments:
            reason = f"the run's execution cap of {max_experiments} experiments was reached"
        else:
            entry = {
                "experiment_id": eid,
                "hypothesis_id": exp.hypothesis_id,
                "analysis_id": exp.analysis_id,
                "tested_claims": list(exp.tested_claims),
                "dataset_refs": list(exp.dataset_refs),
                "feasibility": row["status"],
            }
            hypotheses_on.setdefault(key, set()).add(exp.hypothesis_id)
            if runner is None:
                computed_by[key] = entry
                computations += 1
                entry["selection_reason"] = "bought EXP with the registered handler and supported inputs"
            else:
                entry["result_of"] = runner["experiment_id"]
                entry["selection_reason"] = (
                    f"reuses the verified result of {runner['experiment_id']}: " "same analysis on the same inputs"
                )
            selected.append(entry)
            continue
        skipped.append({"experiment_id": eid, "reason": reason})
    return selected, skipped


def _computation_key(analysis_id: str, dataset_refs: list[str], row: Mapping[str, Any]) -> tuple[str, tuple[str, ...]]:
    """The analysis and the hashes of what it reads: the feasibility row's `sources`, which name each input's
    sha256, or — when a row records none — the EXP's own dataset references, which carry a hash in `@...`."""
    hashes = [str(source.get("sha256")) for source in row.get("sources") or [] if source.get("sha256")]
    return str(analysis_id), tuple(sorted(hashes or dataset_refs))


def check_interpretation(output: ExecuteOutput, selected: Mapping[str, Any], artifact_dir: Path) -> dict[str, Any]:
    """The agent cannot certify its own result or change the preregistered meaning of the outcome."""
    receipt = artifact_dir / "verification.json"
    summary_path = artifact_dir / "summary.json"
    verify_receipt(receipt)
    summary = json.loads(summary_path.read_text())
    # The verified artifact names its analysis; the preregistered meaning of its outcome is that handler's.
    analysis_id = summary["analysis_id"]
    expected = HANDLERS[analysis_id].interpretations[summary["outcome"]]
    errors = []
    if selected.get("analysis_id", analysis_id) != analysis_id:
        errors.append("the verified artifact belongs to another analysis than the selected EXP")
    for field, expected_value in (
        ("experiment_id", selected["experiment_id"]),
        ("analysis_id", analysis_id),
        ("result_digest", summary["result_digest"]),
        ("outcome", summary["outcome"]),
        ("interpretation", expected),
    ):
        if getattr(output, field) != expected_value:
            errors.append(f"{field} disagrees with the verified artifact or preregistered rule")
    for limitation in summary["limitations"]:
        if limitation not in output.limitations:
            errors.append(f"missing limitation: {limitation}")
    return {
        "faithful": not errors,
        "errors": errors,
        "verified_result_digest": summary["result_digest"],
        "outcome": summary["outcome"],
    }
