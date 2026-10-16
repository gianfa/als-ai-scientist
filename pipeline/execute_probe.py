"""`execute` alone, with a live agent, on a delivery case from `tests/fixtures/execute/cases/`.

    UV_PROJECT_ENVIRONMENT=.venv uv run --frozen python -m pipeline.execute_probe \
        tests/fixtures/execute/cases/ics_cytokine_skew_v1.json [--max-cost-usd 2]

A production run reaches `execute` only when the arbiter buys an EXP naming a registered analysis handler
(`analysis/experiment_runner.py::HANDLERS`), and no run guarantees that: an earlier run bought six EXPs and none
named one, so `execute` never launched an agent at all. This probe isolates the phase. A case names everything
`_execute_phase` reads — a knowledge base already holding a bought `Decision` and a matching `Experiment`, and
the local sources that `Experiment` declares — so it depends on no archived run and on no shared store; a new
edge case (for example, `hla_panel_compact_v1`) is a new case file naming a different KB and sources, with no
code change.

Unlike `reassess`, `execute`'s one agent makes a real, consequential tool call: `mcp__experiment__run_experiment`
genuinely runs the selected handler on whatever the case's sources contain. The probe therefore does not fabricate
a result; it fabricates the occasion for one, on data this case controls end to end, and lets the real handler
and the real agent produce it.

A case is a JSON object:

    {"run_id": "...", "kb": "<dir>", "experiment_id": "EXP-1",
     "sources": [{"id": "...", "path": "<file or dir>", "kind": "raw|table|..."}, ...], "note": "..."}

with `kb` and every source `path` relative to the case file. The probe builds this run's own data manifest and
feasibility row from the real `tools.data.local_sources.manifest` and `pipeline.experiment_feasibility.assess` —
the same functions `select()`/`_execute_phase` use — rather than reimplementing their eligibility rules a second
time, so the probe's pre-flight check cannot drift from what `execute` actually assumes. It then copies the
case's knowledge base into a fresh directory and runs the phase there under the case's run ID, because `execute`
selects a bought EXP of that run. The literature store is an empty directory of the probe's own, and the spend
cap is the probe's, not the production run's; `execute`'s own prompt reads no literature and has a zero web
budget, so no substitute verifier is needed (unlike `reassess_probe`).

Exit status: 0 when the phase reaches `executed` with a faithful interpretation, 1 when it does not, 2 when the
case is not a valid delivery.
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

from knowledge_base.schemas import Decision, Experiment
from knowledge_base.store import LOCK_FILE, read_records
from pipeline.experiment_execution import select_all
from pipeline.experiment_feasibility import assess
from tools.data.local_sources import LocalSource, MissingSource, manifest

from .config import DEFAULT_CONFIG, PipelineConfig, load_config
from .launcher import ClaudeLauncher, Launcher
from .orchestrator import REPO_ROOT, Orchestrator

DEFAULT_MAX_COST_USD = 2.0  # guess: no recorded live execute-phase-alone cost exists (no run ever had a bought,
# handler-matching EXP); one tool call plus a short structured output should cost
# well under 1 USD, as reassess's own live sessions did (~0.2-0.4 USD each)


class InvalidDelivery(ValueError):
    """The case is not something `execute` could select and run."""


@dataclass(frozen=True)
class Case:
    name: str
    run_id: str
    kb: Path
    experiment_id: str
    sources: tuple[LocalSource, ...]


@dataclass(frozen=True)
class Probe:
    case: Case
    run_dir: Path
    kb_root: Path
    store: Path


def load_case(path: Path) -> Case:
    data = json.loads(path.read_text())
    sources = tuple(
        LocalSource(
            id=entry["id"],
            path=(path.parent / entry["path"]).resolve(),
            kind=entry.get("kind", "raw"),
            license_note="fixture case",
        )
        for entry in data["sources"]
    )
    return Case(
        name=path.stem,
        run_id=data["run_id"],
        kb=(path.parent / data["kb"]).resolve(),
        experiment_id=data["experiment_id"],
        sources=sources,
    )


def check_selection(case: Case, manifest_data: dict[str, Any]) -> list[str]:
    """Every reason `execute` would not select `case.experiment_id`, by calling the real `select_all`/`assess`
    the phase itself calls — not a replica of their rules, so this check cannot disagree with `execute`.

    The cap is the number of the run's own experiments, so a case is never refused for a reason the production
    cap would give: what this probe tests is one case's EXP, not how many a run may execute.
    """
    records = read_records(case.kb)
    experiments = {rid: r for rid, r in records.items() if isinstance(r, Experiment) and r.run_id == case.run_id}
    if not any(
        isinstance(r, Decision)
        and r.run_id == case.run_id
        and r.decision == "buy"
        and case.experiment_id in r.experiment_ids
        for r in records.values()
    ):
        return [f"no 'buy' decision of run {case.run_id} names {case.experiment_id}"]
    feasibility = {eid: assess(exp, manifest_data) for eid, exp in experiments.items()}
    selected, skipped = select_all(records, case.run_id, feasibility, max(len(experiments), 1))
    if not selected:
        return [f"select_all() chose no EXP: {skipped}"]
    chosen = [row["experiment_id"] for row in selected]
    if case.experiment_id not in chosen:
        return [f"select_all() chose {', '.join(chosen)}, not the case's {case.experiment_id}"]
    return []


def prepare(case: Case, out_root: Path, now: datetime) -> Probe:
    """A fresh probe directory holding the case's knowledge base, data manifest and feasibility row.

    Raises `InvalidDelivery` before anything is written when `execute` would not select `case.experiment_id`.
    """
    try:
        manifest_data = manifest(case.sources, REPO_ROOT)
    except MissingSource as err:
        raise InvalidDelivery(str(err)) from err
    if reasons := check_selection(case, manifest_data):
        raise InvalidDelivery("; ".join(reasons))
    root = out_root / f"probe-execute-{case.name}-{now.strftime('%Y%m%d-%H%M%S')}"
    run_dir, kb_root, store = root / "results", root / "kb", root / "literature"
    run_dir.mkdir(parents=True)  # fails if it exists: a probe never overwrites another
    store.mkdir()
    shutil.copytree(case.kb, kb_root, ignore=shutil.ignore_patterns(LOCK_FILE))
    (run_dir / "data-manifest.json").write_text(json.dumps(manifest_data, indent=2) + "\n")
    records = read_records(case.kb)
    experiment = records[case.experiment_id]
    feasibility = {
        "run_id": case.run_id,
        "manifest": "data-manifest.json",
        "experiments": {case.experiment_id: assess(experiment, manifest_data)},
        "refused": [],
    }
    # Step "00": fabricated by the probe, never a real `design` step, so a reader of the archive sees that at a
    # glance. `_execute_phase` only globs `*-design-feasibility.json`; it does not care about the step number.
    (run_dir / "00-design-feasibility.json").write_text(json.dumps(feasibility, indent=2) + "\n")
    return Probe(case, run_dir, kb_root, store)


def run(
    probe: Probe, config: PipelineConfig, launcher_factory: Callable[..., Launcher] = ClaudeLauncher, commit: str = ""
) -> dict[str, Any]:
    """Launch `execute` alone and return its report. Writes `probe.json` beside it, for provenance."""
    config = config.model_copy(update={"literature_store": probe.store})
    (probe.run_dir / "probe.json").write_text(
        json.dumps(
            {
                "case": probe.case.name,
                "phase": "execute",
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
        config, probe.kb_root, probe.run_dir, probe.case.run_id, launcher_factory(config, probe.kb_root)
    )
    return orchestrator.run(("execute",))["phases"]["execute"]


def probe_config(path: Path, max_cost_usd: float) -> PipelineConfig:
    """The production config, with the probe's own spend cap: the executor's model and prompt are what is tested."""
    return load_config(path).model_copy(update={"run_max_cost_usd": max_cost_usd})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("case", type=Path, help="a case file, e.g. tests/fixtures/execute/cases/*.json")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--max-cost-usd", type=float, default=DEFAULT_MAX_COST_USD)
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "experiments")
    args = parser.parse_args(argv)
    case = load_case(args.case.resolve())
    try:
        probe = prepare(case, args.out.resolve(), datetime.now(timezone.utc))
    except InvalidDelivery as err:
        print(f"execute-probe: {case.name} is not a valid delivery: {err}", file=sys.stderr)
        return 2
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True).stdout.strip()
    print(
        f"execute-probe: {case.name}, run {case.run_id}, PAID (one execute session, cap " f"{args.max_cost_usd} USD)",
        file=sys.stderr,
    )
    report = run(probe, probe_config(args.config, args.max_cost_usd), commit=commit)
    # The phase may execute several EXPs; this probe is about the case's own, so its row is the verdict.
    row = next((r for r in report.get("executions") or [] if r["experiment_id"] == case.experiment_id), {})
    check = row.get("interpretation_check") or {}
    print(
        json.dumps(
            {
                "case": case.name,
                "status": report.get("status"),
                "reason": report.get("reason"),
                "experiment_id": case.experiment_id,
                "execution_status": row.get("status"),
                "execution_reason": row.get("reason"),
                "faithful": check.get("faithful"),
                "errors": check.get("errors"),
                "outcome": row.get("result_digest") and check.get("outcome"),
                "cost_usd": (report.get("launch") or {}).get("session_cost_usd"),
                "results": str(probe.run_dir),
            },
            indent=2,
        )
    )
    return 0 if row.get("status") == "executed" and check.get("faithful") else 1


if __name__ == "__main__":
    sys.exit(main())
