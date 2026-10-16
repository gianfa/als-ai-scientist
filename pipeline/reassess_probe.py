"""`reassess` alone, with a live agent, on a delivery read from a fixture case rather than from a run.

    UV_PROJECT_ENVIRONMENT=.venv uv run --frozen python -m pipeline.reassess_probe \
        tests/fixtures/reassess/cases/repeated_execution.json [--max-cost-usd 2]

`reassess` runs only after `execute` has verified an experiment, and a full run reaches that point only when the
arbiter buys an EXP with a registered handler: an earlier run bought six and none had one, so 11.83 USD bought no
observation of the phase it was launched to observe. This probe isolates the phase. A case names everything
`reassess` reads — the execute report, the run it belongs to, a minimal knowledge base and the experiment's
artifact — so it depends on no archived run and on no shared store, and new edge cases are new case files.

A case is a JSON object:

    {"run_id": "...", "kb": "<dir>", "artifacts": "<dir>", "note": "...", "execute_report": {...}}

with `kb` and `artifacts` relative to the case file. The probe checks the report against what `reassess` reads,
copies the knowledge base and the artifacts into a fresh directory, and runs the phase there under the case's
run ID, because `reassess` judges a hypothesis of the run that executed the experiment. The literature store is an
empty directory of the probe's own and the spend cap is the probe's, not the production run's.

Exit status: 0 when the phase reaches `judged`, 1 when it does not, 2 when the case is not a valid delivery.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from knowledge_base.schemas import Claim, Experiment, Hypothesis
from knowledge_base.status import superseded
from knowledge_base.store import LOCK_FILE, read_records
from knowledge_base.validate import Record

from .config import DEFAULT_CONFIG, PipelineConfig, load_config
from .launcher import ClaudeLauncher, Launcher
from .orchestrator import REPO_ROOT, Orchestrator

DIGEST = re.compile(r"^[0-9a-f]{64}$")  # the pattern of `proposals.Digest`, which `reassess` compares against
DEFAULT_MAX_COST_USD = 2.0  # one reassess session cost 0.40 USD on an earlier run


class InvalidDelivery(ValueError):
    """The case is not something `reassess` could judge."""


@dataclass(frozen=True)
class Case:
    name: str
    run_id: str
    kb: Path
    artifacts: Path
    execute_report: dict[str, Any]


@dataclass(frozen=True)
class Probe:
    case: Case
    run_dir: Path
    kb_root: Path
    store: Path


def load_case(path: Path) -> Case:
    data = json.loads(path.read_text())
    return Case(
        name=path.stem,
        run_id=data["run_id"],
        kb=(path.parent / data["kb"]).resolve(),
        artifacts=(path.parent / data["artifacts"]).resolve(),
        execute_report=data["execute_report"],
    )


def check_delivery(report: dict[str, Any], records: dict[str, Record], run_id: str) -> list[str]:
    """Every reason `report` is not a delivery `reassess` can judge over `records`; empty when it is.

    The checks are the fields `Orchestrator._reassess_phase` reads and what it assumes of them, so a probe that
    passes here fails, if at all, on the agent's judgement and not on its input. A report
    carries one row per executed EXP (`executions`), and every executed row is checked: a delivery with two
    good rows and one broken one is not a delivery.
    """
    if report.get("status") != "executed":
        return [f"status is {report.get('status')!r}, not 'executed'"]
    executions = report.get("executions")
    if not isinstance(executions, list) or not executions:
        return ["executions must be a non-empty list of execution rows"]
    executed = [row for row in executions if isinstance(row, dict) and row.get("status") == "executed"]
    if not executed:
        return ["no execution row has status 'executed'"]
    return [reason for row in executed for reason in _check_execution(row, records, run_id)]


def _check_execution(row: dict[str, Any], records: dict[str, Record], run_id: str) -> list[str]:
    """The same checks, for one executed EXP; every reason names the EXP so a reader knows which row failed."""
    reasons: list[str] = []
    eid, hid = row.get("experiment_id"), row.get("hypothesis_id")
    where = f"{eid}: "
    if not isinstance(records.get(eid), Experiment):
        reasons.append(f"{where}selected experiment {eid!r} is not an experiment in the knowledge base")
    hypothesis = records.get(hid)
    if not isinstance(hypothesis, Hypothesis):
        reasons.append(f"{where}selected hypothesis {hid!r} is not a hypothesis in the knowledge base")
    elif hypothesis.run_id != run_id:
        reasons.append(f"{where}hypothesis {hid} belongs to run {hypothesis.run_id}, not {run_id}")
    elif hid in superseded(records):
        reasons.append(f"{where}hypothesis {hid} is superseded, so `reassess` would not find its context")
    if not row.get("tested_claims"):
        reasons.append(f"{where}selected EXP names no tested claims")
    digest = row.get("result_digest")
    if not isinstance(digest, str) or not DIGEST.match(digest):
        reasons.append(f"{where}result_digest is not a bare 64-character hex digest")
    verified = (row.get("interpretation_check") or {}).get("verified_result_digest")
    if verified is not None and verified != digest:
        reasons.append(f"{where}result_digest differs from the digest the interpretation check verified")
    written, seen_again = row.get("written"), row.get("seen_again")
    if not isinstance(written, list) or not isinstance(seen_again, dict):
        return reasons + [f"{where}written must be a list and seen_again a map"]
    result_claims = [*written, *seen_again.values()]
    if not result_claims:
        reasons.append(f"{where}the execution holds no claim: written and seen_again are both empty")
    reasons += [
        f"{where}result claim {cid} is not a claim in the knowledge base"
        for cid in result_claims
        if not isinstance(records.get(cid), Claim)
    ]
    reasons += [
        f"{where}seen_again key {cid} is in the knowledge base, so it is not a dropped ID"
        for cid in seen_again
        if cid in records
    ]
    return reasons


def prepare(case: Case, out_root: Path, now: datetime) -> Probe:
    """A fresh probe directory holding the case's execute report, its artifacts and a copy of its knowledge base.

    Raises `InvalidDelivery` before anything is written when the case is not a valid delivery.
    """
    if reasons := check_delivery(case.execute_report, read_records(case.kb), case.run_id):
        raise InvalidDelivery("; ".join(reasons))
    root = out_root / f"probe-reassess-{case.name}-{now.strftime('%Y%m%d-%H%M%S')}"
    run_dir, kb_root, store = root / "results", root / "kb", root / "literature"
    run_dir.mkdir(parents=True)  # fails if it exists: a probe never overwrites another
    store.mkdir()
    shutil.copytree(case.kb, kb_root, ignore=shutil.ignore_patterns(LOCK_FILE))
    if case.artifacts.is_dir():  # the report names the artifact, and an agent may read it
        shutil.copytree(case.artifacts, run_dir / "experiment-analysis")
    (run_dir / "09-execute-report.json").write_text(json.dumps(case.execute_report, indent=2) + "\n")
    return Probe(case, run_dir, kb_root, store)


def _no_quote_verification(claim: Any) -> Any:
    raise RuntimeError("reassess verifies no quote")


def run(
    probe: Probe, config: PipelineConfig, launcher_factory: Callable[..., Launcher] = ClaudeLauncher, commit: str = ""
) -> dict[str, Any]:
    """Launch `reassess` alone and return its report. Writes `probe.json` beside it, for provenance."""
    config = config.model_copy(update={"literature_store": probe.store})
    (probe.run_dir / "probe.json").write_text(
        json.dumps(
            {
                "case": probe.case.name,
                "phase": "reassess",
                "run_id": probe.case.run_id,
                "kb": str(probe.kb_root),
                "config_hash": config.config_hash,
                "run_max_cost_usd": config.run_max_cost_usd,
                "git_commit": commit,
                "started_at": datetime.now(timezone.utc).isoformat(),
            },
            indent=2,
        )
        + "\n"
    )
    # A verifier is injected so the orchestrator opens no literature store: `reassess` checks no quote.
    orchestrator = Orchestrator(
        config,
        probe.kb_root,
        probe.run_dir,
        probe.case.run_id,
        launcher_factory(config, probe.kb_root),
        verifier=_no_quote_verification,
    )
    return orchestrator.run(("reassess",))["phases"]["reassess"]


def probe_config(path: Path, max_cost_usd: float) -> PipelineConfig:
    """The production config, with the probe's own spend cap: the reassessor's model and prompt are what is tested."""
    return load_config(path).model_copy(update={"run_max_cost_usd": max_cost_usd})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("case", type=Path, help="a case file, e.g. tests/fixtures/reassess/cases/*.json")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--max-cost-usd", type=float, default=DEFAULT_MAX_COST_USD)
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "experiments")
    args = parser.parse_args(argv)
    case = load_case(args.case.resolve())
    try:
        probe = prepare(case, args.out.resolve(), datetime.now(timezone.utc))
    except InvalidDelivery as err:
        print(f"reassess-probe: {case.name} is not a valid delivery: {err}", file=sys.stderr)
        return 2
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True).stdout.strip()
    print(
        f"reassess-probe: {case.name}, run {case.run_id}, PAID (one reassess session, cap " f"{args.max_cost_usd} USD)",
        file=sys.stderr,
    )
    report = run(probe, probe_config(args.config, args.max_cost_usd), commit=commit)
    # One judgement per executed EXP: each is reported with its own status, so a case that delivers two
    # executions and gets one judgement bound cannot read as a success.
    judgements = [
        {
            "experiment_id": entry.get("experiment_id"),
            "status": entry.get("status"),
            "error": entry.get("error"),
            "decision": (entry.get("judgement") or {}).get("decision"),
            "claim_ids": (entry.get("judgement") or {}).get("claim_ids"),
        }
        for entry in report.get("judgements") or []
    ]
    print(
        json.dumps(
            {
                "case": case.name,
                "status": report.get("status"),
                "reason": report.get("reason"),
                "judged_count": report.get("judged_count"),
                "judgements": judgements,
                "cost_usd": (report.get("launch") or {}).get("session_cost_usd"),
                "results": str(probe.run_dir),
            },
            indent=2,
        )
    )
    return 0 if judgements and all(entry["status"] == "judged" for entry in judgements) else 1


if __name__ == "__main__":
    sys.exit(main())
