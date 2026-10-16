"""`design` then `arbitrate`, with live agents, on a case that offers only one registered handler's data.

    UV_PROJECT_ENVIRONMENT=.venv uv run --frozen python -m pipeline.design_arbitrate_probe \
        tests/fixtures/design_arbitrate/cases/ics_cytokine_skew_v1_only_data.json [--max-cost-usd 4]

Neither `reassess_probe` nor `execute_probe` observes whether an agent *chooses* a registered handler: both
start from a `Decision`/`Experiment` already written into the fixture. This probe isolates the step before
both — proposal and purchase — with no `Experiment` or `Decision` pre-written: that is exactly the outcome it
observes, not something it fabricates.

A case's knowledge base holds one hypothesis with an unresolved target claim and nothing else; its data sources
are deliberately only the inputs one registered handler needs, never a second handler's. A `design` agent
cannot propose the other handler as `data_supported` here because the data for it is simply not declared in
this run's manifest, not because anything tells it not to — the same condition a production run would be in if
an input were undeclared. `author_confidence` is irrelevant to this and is not used to bias the outcome: the
agents never see it (`knowledge_base.retrieve.BLINDED`).

This does **not** guarantee the agent proposes or buys the named handler. A real `design` agent may propose a
wet-lab experiment instead, or none; a real `arbiter` may reject or stop. The probe reports what happened — did
the handler get proposed, was it `data_supported`, did `arbitrate` buy it — and asserts only what is true by
construction, never a specific judgement call.

Exit status: 0 when the probe ran both phases and wrote a report (regardless of what was decided), 2 when the
case cannot even be prepared (a missing source).
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
from tools.data.local_sources import LocalSource, MissingSource, manifest

from .config import DEFAULT_CONFIG, PipelineConfig, load_config
from .launcher import ClaudeLauncher, Launcher
from .orchestrator import REPO_ROOT, Orchestrator

DEFAULT_MAX_COST_USD = 4.0  # two agent phases rather than one; a guess, generous against reassess/execute's
# observed ~0.2-0.4 USD each


class InvalidCase(ValueError):
    """The case's own sources cannot be read, before any agent is involved."""


@dataclass(frozen=True)
class Case:
    name: str
    run_id: str
    kb: Path
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
    return Case(name=path.stem, run_id=data["run_id"], kb=(path.parent / data["kb"]).resolve(), sources=sources)


def prepare(case: Case, out_root: Path, now: datetime) -> Probe:
    """A fresh probe directory holding the case's knowledge base and a data manifest naming only its sources."""
    try:
        manifest_data = manifest(case.sources, REPO_ROOT)
    except MissingSource as err:
        raise InvalidCase(str(err)) from err
    root = out_root / f"probe-design-arbitrate-{case.name}-{now.strftime('%Y%m%d-%H%M%S')}"
    run_dir, kb_root, store = root / "results", root / "kb", root / "literature"
    run_dir.mkdir(parents=True)  # fails if it exists: a probe never overwrites another
    store.mkdir()
    shutil.copytree(case.kb, kb_root, ignore=shutil.ignore_patterns(LOCK_FILE))
    (run_dir / "data-manifest.json").write_text(json.dumps(manifest_data, indent=2) + "\n")
    return Probe(case, run_dir, kb_root, store)


def outcome(probe: Probe, analysis_id: str = "ics_cytokine_skew_v1") -> dict[str, Any]:
    """What happened, read from the probe's own copy of the knowledge base and its archived feasibility report.

    Never asserts a specific judgement call: only what is checkable by construction (an EXP was or was not
    written naming the handler; it was or was not `data_supported`; a `Decision` buying it was or was not made).
    """
    records = read_records(probe.kb_root)
    proposed = [
        r
        for r in records.values()
        if isinstance(r, Experiment) and r.run_id == probe.case.run_id and r.analysis_id == analysis_id
    ]
    feasibility_path = next(iter(sorted(probe.run_dir.glob("*-design-feasibility.json"))), None)
    feasibility = json.loads(feasibility_path.read_text())["experiments"] if feasibility_path else {}
    data_supported = [e.id for e in proposed if feasibility.get(e.id, {}).get("status") == "data_supported"]
    bought = [
        eid
        for r in records.values()
        if isinstance(r, Decision) and r.run_id == probe.case.run_id and r.decision == "buy"
        for eid in r.experiment_ids
    ]
    decisions = [
        {"id": r.id, "decision": r.decision, "experiment_ids": r.experiment_ids}
        for r in records.values()
        if isinstance(r, Decision) and r.run_id == probe.case.run_id
    ]
    return {
        "proposed_experiment_ids": [e.id for e in proposed],
        "data_supported_experiment_ids": data_supported,
        "bought_experiment_ids": bought,
        "handler_bought": bool(set(data_supported) & set(bought)),
        "decisions": decisions,
    }


def run(
    probe: Probe, config: PipelineConfig, launcher_factory: Callable[..., Launcher] = ClaudeLauncher, commit: str = ""
) -> dict[str, Any]:
    """Launch `design` then `arbitrate` and return `{"design": ..., "arbitrate": ..., "outcome": ...}`."""
    config = config.model_copy(update={"literature_store": probe.store})
    (probe.run_dir / "probe.json").write_text(
        json.dumps(
            {
                "case": probe.case.name,
                "phases": ["design", "arbitrate"],
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
    state = orchestrator.run(("design", "arbitrate"))
    return {"design": state["phases"]["design"], "arbitrate": state["phases"]["arbitrate"], "outcome": outcome(probe)}


def probe_config(path: Path, max_cost_usd: float) -> PipelineConfig:
    return load_config(path).model_copy(update={"run_max_cost_usd": max_cost_usd})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("case", type=Path, help="a case file, e.g. tests/fixtures/design_arbitrate/cases/*.json")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--max-cost-usd", type=float, default=DEFAULT_MAX_COST_USD)
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "experiments")
    args = parser.parse_args(argv)
    case = load_case(args.case.resolve())
    try:
        probe = prepare(case, args.out.resolve(), datetime.now(timezone.utc))
    except InvalidCase as err:
        print(f"design-arbitrate-probe: {case.name}: {err}", file=sys.stderr)
        return 2
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True).stdout.strip()
    print(
        f"design-arbitrate-probe: {case.name}, run {case.run_id}, PAID (two agent phases, cap "
        f"{args.max_cost_usd} USD)",
        file=sys.stderr,
    )
    report = run(probe, probe_config(args.config, args.max_cost_usd), commit=commit)
    print(
        json.dumps(
            {
                "case": case.name,
                "outcome": report["outcome"],
                "design_cost_usd": (report["design"].get("launch") or {}).get("session_cost_usd"),
                "arbitrate_cost_usd": (report["arbitrate"].get("launch") or {}).get("session_cost_usd"),
                "results": str(probe.run_dir),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
