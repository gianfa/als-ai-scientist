"""`intervene` alone, with a live agent, and the host-side drug lookup it feeds.

    UV_PROJECT_ENVIRONMENT=.venv uv run --frozen python -m pipeline.intervene_drug_lookup_probe \
        tests/fixtures/intervene/cases/surviving_hypothesis.json [--max-cost-usd 2]

Two things the mocked tests cannot show, and that no run has ever shown either, because the step is off by
default (`phases.intervene.drug_lookup_enabled`):

1. that a **live** `intervene` agent fills `drug_name` at all. The prompt asks for it only when the agent is
   sure of a real compound, never as a guess, so the field may legitimately come back empty — which is an
   outcome of this probe, not a defect of it, and is reported as such rather than retried;
2. that the host-side lookup then queries the **real** MyChem.info and commits what it answers. A run reaches
   this only after `intervene` has committed its candidates, so it cannot be exercised by calling the client
   directly: `tests/test_tools_data_pharmacology.py` covers the client, `tests/test_pipeline_drug_lookup.py`
   covers the orchestrator step against a patched `lookup_match`, and neither touches the network.

A case is a JSON object:

    {"run_id": "...", "kb": "<dir>", "note": "..."}

with `kb` relative to the case file. `intervene` reads the hypotheses of `run_id` that the arbiter did not
reject, so a valid case is a knowledge base holding at least one of those; the check calls
`Orchestrator._surviving_items` itself, over a read-only view of the records, rather than restating its rule —
the same reason `execute_probe` calls `select`/`assess` instead of replicating them. The records are read
without opening the knowledge base for writing, so the case's own directory is never locked or modified.

The probe gives the lookup **its own empty cache**, inside the probe directory: a cache shared with the
repository's would let a name cached by an earlier call answer without the network, which is exactly the half
of the path this probe exists to exercise.

Exit status: 0 when a candidate carried a `drug_name` and its lookup committed a claim, 1 when it did not
(including the honest "the agent named no compound"), 2 when the case is not a valid delivery.
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

from knowledge_base.store import LOCK_FILE, Record, read_records

from .config import DEFAULT_CONFIG, PipelineConfig, load_config
from .launcher import ClaudeLauncher, FakeLauncher, Launcher
from .orchestrator import REPO_ROOT, Orchestrator

DEFAULT_MAX_COST_USD = 2.0  # one intervene session over one hypothesis; the live reassess and execute
# sessions of the sibling probes cost 0.19-0.44 USD each

REPORT = "01-intervene-report.json"  # `run(("intervene",))` numbers its single phase 1


class InvalidDelivery(ValueError):
    """The case is not something `intervene` would act on."""


@dataclass(frozen=True)
class Case:
    name: str
    run_id: str
    kb: Path


@dataclass(frozen=True)
class Probe:
    case: Case
    run_dir: Path
    kb_root: Path
    store: Path
    cache: Path


@dataclass(frozen=True)
class _Records:
    """A read-only stand-in for `KnowledgeBase` exposing the one attribute the two selection methods read.

    `KnowledgeBase.open` takes a lock and writes to the directory it opens; a pre-flight check must not do
    either to a fixture it only wants to inspect.
    """

    records: dict[str, Record]


def load_case(path: Path) -> Case:
    data = json.loads(path.read_text())
    return Case(name=path.stem, run_id=data["run_id"], kb=(path.parent / data["kb"]).resolve())


def surviving(case: Case, config: PipelineConfig) -> list[dict[str, Any]]:
    """The hypotheses `intervene` would be given for this case, from the orchestrator's own method.

    Constructing an `Orchestrator` writes nothing — it only keeps the paths it is given — so this check runs
    before the probe directory exists and against a launcher it never reaches.
    """
    orchestrator = Orchestrator(config, case.kb, case.kb, case.run_id, FakeLauncher({}))
    return orchestrator._surviving_items(_Records(read_records(case.kb)), config.phases.intervene.hops)


def prepare(case: Case, out_root: Path, now: datetime, config: PipelineConfig) -> Probe:
    """A fresh probe directory holding a copy of the case's knowledge base and an empty lookup cache.

    Raises `InvalidDelivery` before anything is written when `intervene` would be given no hypothesis.
    """
    if not surviving(case, config):
        raise InvalidDelivery(f"no hypothesis of run {case.run_id} survived the arbiter in {case.kb}")
    root = out_root / f"probe-intervene-drug-{case.name}-{now.strftime('%Y%m%d-%H%M%S')}"
    run_dir, kb_root, store = root / "results", root / "kb", root / "literature"
    run_dir.mkdir(parents=True)  # fails if it exists: a probe never overwrites another
    store.mkdir()
    shutil.copytree(case.kb, kb_root, ignore=shutil.ignore_patterns(LOCK_FILE))
    return Probe(case, run_dir, kb_root, store, root / "cache" / "pharmacology.jsonl")


def probe_config(path: Path, max_cost_usd: float) -> PipelineConfig:
    """The production config, with the probe's own spend cap: the agent's model and prompt are what is tested."""
    return load_config(path).model_copy(update={"run_max_cost_usd": max_cost_usd})


def enable_lookup(config: PipelineConfig, probe: Probe) -> PipelineConfig:
    """The config the probe runs with: the lookup on, its cache the probe's own, the literature store empty."""
    intervene = config.phases.intervene.model_copy(
        update={"drug_lookup_enabled": True, "drug_lookup_cache": probe.cache}
    )
    return config.model_copy(
        update={"literature_store": probe.store, "phases": config.phases.model_copy(update={"intervene": intervene})}
    )


def run(
    probe: Probe, config: PipelineConfig, launcher_factory: Callable[..., Launcher] = ClaudeLauncher, commit: str = ""
) -> dict[str, Any]:
    """Launch `intervene` alone, with the lookup enabled, and return the report it wrote.

    The report is read back off disk rather than taken from `run()`'s return value: `_phase` compacts every
    list and dict it does not list as worth keeping into its own length, and `drug_lookup` is one of them.
    """
    config = enable_lookup(config, probe)
    (probe.run_dir / "probe.json").write_text(
        json.dumps(
            {
                "case": probe.case.name,
                "phase": "intervene",
                "run_id": probe.case.run_id,
                "kb": str(probe.kb_root),
                "drug_lookup_enabled": True,
                "drug_lookup_cache": str(probe.cache),
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
    orchestrator.run(("intervene",))
    return json.loads((probe.run_dir / REPORT).read_text())


def outcome(report: dict[str, Any]) -> dict[str, Any]:
    """What this probe set out to observe, as facts off the report: never a verdict the report does not carry.

    The compound names come from the lookup's own rows, which the host builds from the `Intervention` records
    the phase committed; `report["candidates"]` is a count per hypothesis and carries no name.
    """
    lookups = report.get("drug_lookup") or {}
    return {
        "candidates": sum((report.get("candidates") or {}).values()),
        "drug_names": sorted(lookups),
        "matches": {name: row["match"] for name, row in lookups.items() if "match" in row},
        "claims": {name: row["claim_id"] for name, row in lookups.items() if "claim_id" in row},
        "errors": {name: row["error"] for name, row in lookups.items() if "error" in row},
        "cost_usd": (report.get("launch") or {}).get("session_cost_usd"),
    }


def succeeded(report: dict[str, Any]) -> bool:
    """A pass needs both halves: a name the agent stood behind, and a lookup that answered and was committed."""
    seen = outcome(report)
    return (
        bool(seen["drug_names"])
        and not seen["errors"]
        and set(seen["matches"]) == set(seen["drug_names"]) == set(seen["claims"])
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("case", type=Path, help="a case file, e.g. tests/fixtures/intervene/cases/*.json")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--max-cost-usd", type=float, default=DEFAULT_MAX_COST_USD)
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "experiments")
    args = parser.parse_args(argv)
    case = load_case(args.case.resolve())
    config = probe_config(args.config, args.max_cost_usd)
    try:
        probe = prepare(case, args.out.resolve(), datetime.now(timezone.utc), config)
    except InvalidDelivery as err:
        print(f"intervene-drug-probe: {case.name} is not a valid delivery: {err}", file=sys.stderr)
        return 2
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True).stdout.strip()
    print(
        f"intervene-drug-probe: {case.name}, run {case.run_id}, PAID (one intervene session, cap "
        f"{args.max_cost_usd} USD) and a LIVE MyChem.info query per named compound",
        file=sys.stderr,
    )
    report = run(probe, config, commit=commit)
    print(json.dumps({"case": case.name, **outcome(report), "results": str(probe.run_dir)}, indent=2))
    return 0 if succeeded(report) else 1


if __name__ == "__main__":
    sys.exit(main())
