"""`synthesize` alone, on a case from `tests/fixtures/synthesize/cases/`: the real host code, a scripted or live PI.

    UV_PROJECT_ENVIRONMENT=.venv uv run --frozen python -m pipeline.synthesize_probe \
        tests/fixtures/synthesize/cases/reuse_receipt.json [--live] [--max-cost-usd 2]

an earlier run died at synthesize, after eleven phases and their cost, on a path no test had walked: the receipt of
a reuse. A unit test with a hand-built archive could not show it, because the archive's shape was
whatever the test wrote. This probe builds the archive the way a run does. It copies the case's knowledge base,
declares its data, then runs the **real** `execute` phase — the real handler on the case's synthetic data, writing
the real artifact and receipt, with a scripted stand-in for the executor agent only — and then the **real**
`synthesize` phase: the receipt check, the gate and the writing of `final-proposal.json`.

Both agents are scripted by default. `--live-executor` and `--live-pi` (or `--live` for both) make them real and
paid. Scripted, the probe proves the host does not crash on that archive; it does not prove that a live executor's
or a live PI's output, with their real prompts, schemas and tools, is accepted. Live, the gate's verdict is reported
and not required: a PI may fail the gate, which is a finding about the PI, not about the host.

The case has the shape of `pipeline.execute_probe`'s, and `experiment_id` names the EXP whose handling is under
test: here a reuse, which `check_selection` demands be selected with `result_of`.

Exit status: 0 when `final-proposal.json` is written and every bought EXP is reported executed, 1 when not, 2 when
the case is not a valid delivery.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from knowledge_base.schemas import Experiment
from knowledge_base.store import LOCK_FILE, read_records
from pipeline.experiment_execution import select_all
from pipeline.experiment_feasibility import assess
from tools.data.local_sources import MissingSource, manifest

from .config import DEFAULT_CONFIG, PipelineConfig
from .execute_probe import Case, InvalidDelivery, load_case, probe_config
from .launcher import ClaudeLauncher, Launcher, PhaseRun
from .orchestrator import REPO_ROOT, Orchestrator

DEFAULT_MAX_COST_USD = 2.0  # the scripted default; nothing is spent
DEFAULT_LIVE_MAX_COST_USD = 2.0  # measured on one live run: cost 0.30 (execute) + 0.44 (synthesize) USD;
# about 2.7x that, since a cap too narrow kills a phase and loses its spend


@dataclass(frozen=True)
class Probe:
    case: Case
    run_dir: Path
    kb_root: Path
    store: Path


class SplitLauncher:
    """Routes each phase to its own launcher: the executor stand-in for `execute`, the PI's for `synthesize`."""

    def __init__(self, by_phase: dict[str, Launcher]) -> None:
        self.by_phase = by_phase

    def run(self, phase: str, step: int, args: dict[str, Any], run_dir: Path) -> PhaseRun:
        return self.by_phase[phase].run(phase, step, args, run_dir)


def check_selection(case: Case, manifest_data: dict[str, Any]) -> list[str]:
    """Why this case does not exercise a reuse: the real `select_all`, not a replica of its rules."""
    records = read_records(case.kb)
    experiments = {rid: r for rid, r in records.items() if isinstance(r, Experiment) and r.run_id == case.run_id}
    feasibility = {eid: assess(exp, manifest_data) for eid, exp in experiments.items()}
    selected, skipped = select_all(records, case.run_id, feasibility, max(len(experiments), 1))
    row = next((r for r in selected if r["experiment_id"] == case.experiment_id), None)
    if row is None:
        return [f"select_all() did not choose {case.experiment_id}: {skipped}"]
    if not row.get("result_of"):
        return [f"{case.experiment_id} is a computation, not a reuse: nothing here tests a reuse's receipt"]
    return []


def prepare(case: Case, out_root: Path, now: datetime) -> Probe:
    """A fresh probe directory with the case's knowledge base, data manifest and feasibility rows.

    Raises `InvalidDelivery` before anything is written when the case does not select its EXP as a reuse.
    """
    try:
        manifest_data = manifest(case.sources, REPO_ROOT)
    except MissingSource as err:
        raise InvalidDelivery(str(err)) from err
    if reasons := check_selection(case, manifest_data):
        raise InvalidDelivery("; ".join(reasons))
    root = out_root / f"probe-synthesize-{case.name}-{now.strftime('%Y%m%d-%H%M%S')}"
    run_dir, kb_root, store = root / "results", root / "kb", root / "literature"
    run_dir.mkdir(parents=True)  # fails if it exists: a probe never overwrites another
    store.mkdir()
    shutil.copytree(case.kb, kb_root, ignore=shutil.ignore_patterns(LOCK_FILE))
    (run_dir / "data-manifest.json").write_text(json.dumps(manifest_data, indent=2) + "\n")
    records = read_records(case.kb)
    experiments = {rid: r for rid, r in records.items() if isinstance(r, Experiment) and r.run_id == case.run_id}
    feasibility = {
        "run_id": case.run_id,
        "manifest": "data-manifest.json",
        "experiments": {eid: assess(exp, manifest_data) for eid, exp in experiments.items()},
        "refused": [],
    }
    # Step "00": written by the probe, never a real `design` step, so a reader of the archive sees that at a glance.
    (run_dir / "00-design-feasibility.json").write_text(json.dumps(feasibility, indent=2) + "\n")
    return Probe(case, run_dir, kb_root, store)


def run(probe: Probe, config: PipelineConfig, executor: Launcher, pi: Launcher, commit: str = "") -> dict[str, Any]:
    """`execute` then `synthesize` on the probe's directory; the state of the run. Writes `probe.json`."""
    config = config.model_copy(update={"literature_store": probe.store})
    (probe.run_dir / "probe.json").write_text(
        json.dumps(
            {
                "case": probe.case.name,
                "phases": ["execute", "synthesize"],
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
    orchestrator = Orchestrator(
        config, probe.kb_root, probe.run_dir, probe.case.run_id, SplitLauncher({"execute": executor, "synthesize": pi})
    )
    return orchestrator.run(("execute", "synthesize"))


def verdict(probe: Probe, state: dict[str, Any]) -> dict[str, Any]:
    """What the exit status reads, and what a live failure needs: the proposal on disk, the executions it was
    written from, each phase's rejected outputs and cost, all taken from the persisted reports."""
    path = probe.run_dir / "final-proposal.json"
    execute, synthesize = state["phases"]["execute"], state["phases"].get("synthesize") or {}
    proposal = json.loads(path.read_text()) if path.is_file() else None

    def cost(report: dict[str, Any]) -> float | None:
        return (report.get("launch") or {}).get("session_cost_usd")

    return {
        "case": probe.case.name,
        "proposal_written": proposal is not None,
        "gate": proposal and proposal["gate"],
        "executions": {
            row["experiment_id"]: {
                "status": row["status"],
                "result_of": row.get("result_of"),
                "faithful": (row.get("interpretation_check") or {}).get("faithful"),
                "errors": (row.get("interpretation_check") or {}).get("errors"),
            }
            for row in execute.get("executions") or []
        },
        "synthesize": {
            "invalid_outputs": synthesize.get("invalid_outputs"),
            "dropped_agents": synthesize.get("dropped_agents"),
            "skipped": synthesize.get("skipped"),
        },
        "cost_usd": {"execute": cost(execute), "synthesize": cost(synthesize)},
        "results": str(probe.run_dir),
    }


INTERPRETATIONS = {"skew_replicated": "supports_ifng_skew_only", "skew_not_replicated": "weakens_ifng_skew_only"}


def scripted_executor(run_dir: Path) -> Launcher:
    """Stands in for the executor agents: one per computed EXP, each calling the real MCP tool on the case's data.

    Only the agent is replaced. The handler runs, writes the artifact and the receipt, and the host verifies them.
    Written for `ics_cytokine_skew_v1`; another analysis needs its outcomes added to `INTERPRETATIONS`.
    """
    from tools.experiment.mcp_server import run_experiment

    def execute(args: dict[str, Any]) -> dict[str, Any]:
        outputs = []
        for selected in args["selected"]:
            result = run_experiment(run_dir / "execution-policy.json", selected["experiment_id"])
            if "error" in result:
                raise RuntimeError(f"{selected['experiment_id']}: {result['error']}")
            outputs.append(
                {
                    "agent_id": f"executor@{selected['experiment_id']}",
                    "output": {
                        "experiment_id": result["experiment_id"],
                        "analysis_id": result["analysis_id"],
                        "result_digest": result["result_digest"],
                        "outcome": result["outcome"],
                        "interpretation": INTERPRETATIONS[result["outcome"]],
                        "limitations": result["limitations"],
                    },
                }
            )
        return {"outputs": outputs}

    return _Scripted({"execute": execute})


def scripted_pi_output(args: dict[str, Any]) -> dict[str, Any]:
    """A proposal citing every hypothesis of the run and calling performed exactly the EXPs that executed."""
    hypotheses = [h["hypothesis_id"] for h in args["items"][0]["hypotheses"]]
    performed = [
        row["experiment_id"] for row in args["execution"].get("executions", []) if row.get("status") == "executed"
    ]
    cites = [*hypotheses, *performed]
    section = {"text": "Synthetic fixture text " + " ".join(f"[[{i}]]" for i in cites) + ".", "cites": cites}
    return {
        "outputs": [
            {
                "agent_id": "pi",
                "output": {
                    "outcome": "proposal",
                    "summary": "Scripted proposal for the synthesize probe.",
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
                    "performed_experiment_ids": performed,
                    "failure_modes": [{"text": f"Failure {n}.", "cites": []} for n in range(5)],
                },
            }
        ]
    }


class _Scripted:
    """`responses[phase](args) -> workflow result`, without recording: the probe reads the archive instead."""

    def __init__(self, responses: dict[str, Callable[[dict[str, Any]], dict[str, Any]]]) -> None:
        self.responses = responses

    def run(self, phase: str, step: int, args: dict[str, Any], run_dir: Path) -> PhaseRun:
        return PhaseRun(self.responses[phase](args), {"scripted": True})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("case", type=Path)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--live", action="store_true", help="live executor and live PI (paid): both flags below")
    parser.add_argument("--live-executor", action="store_true", help="a live executor agent per computed EXP (paid)")
    parser.add_argument("--live-pi", action="store_true", help="a live PI session (paid)")
    parser.add_argument(
        "--max-cost-usd",
        type=float,
        default=None,
        help=f"the spend cap shared by both phases; default {DEFAULT_MAX_COST_USD} scripted, "
        f"{DEFAULT_LIVE_MAX_COST_USD} when anything is live",
    )
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "experiments")
    args = parser.parse_args(argv)
    case = load_case(args.case.resolve())
    try:
        probe = prepare(case, args.out.resolve(), datetime.now(timezone.utc))
    except InvalidDelivery as err:
        print(f"synthesize-probe: {case.name} is not a valid delivery: {err}", file=sys.stderr)
        return 2
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True).stdout.strip()
    live_executor, live_pi = args.live or args.live_executor, args.live or args.live_pi
    cap = args.max_cost_usd or (DEFAULT_LIVE_MAX_COST_USD if live_executor or live_pi else DEFAULT_MAX_COST_USD)
    config = probe_config(args.config, cap)
    executor: Launcher = ClaudeLauncher(config, probe.kb_root) if live_executor else scripted_executor(probe.run_dir)
    pi: Launcher = ClaudeLauncher(config, probe.kb_root) if live_pi else _Scripted({"synthesize": scripted_pi_output})
    live = [name for name, on in (("executor", live_executor), ("PI", live_pi)) if on]
    print(
        f"synthesize-probe: {case.name}, run {case.run_id}, "
        f"{'PAID, live ' + ' and '.join(live) + f', cap {cap} USD' if live else 'both agents scripted'}",
        file=sys.stderr,
    )
    state = run(probe, config, executor, pi, commit=commit)
    result = verdict(probe, state)
    print(json.dumps(result, indent=2))
    ok = result["proposal_written"] and all(r["status"] == "executed" for r in result["executions"].values())
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
