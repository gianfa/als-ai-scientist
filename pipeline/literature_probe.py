"""`literature` alone, with live agents, on a fixture store that deliberately holds nothing.

    UV_PROJECT_ENVIRONMENT=.venv uv run --frozen python -m pipeline.literature_probe \
        tests/fixtures/literature/cases/fetch_a_paper_it_found.json [--max-cost-usd 3]

Why this exists. `fetch_paper` was built from a measurement: 192 of the agents' 760 archived
`check_quote` calls were refused `paper_not_in_store` on a resolvable identifier, and 85 of those papers never
reached a claim at all. Every test of the tool is offline, with injected transports and a fake agent. None of
them answers the question the measurement actually raises: **does a real agent, told it may fetch, do it?**
That is a fact about an agent's behaviour, and only a live session can establish it.

What is a fixture here and what is real. The knowledge base, the literature store, the questions and the spend
cap are the probe's; the store is created **empty**, so every `check_quote` must come back
`paper_not_in_store` and the only way forward is the tool. The agents, the MCP servers, the quote check and
Europe PMC/OpenAlex are real: the paper is really downloaded and the quote is really verified against the text
that arrived. The retry policy is **not** exercised here — a 429 cannot be provoked on demand, and
`tests/test_tools_literature_fetch.py` covers it offline, including the real `urllib` path reading
`Retry-After` from a local server.

A case is a JSON object:

    {"run_id": "...", "kb": "<dir>", "questions": ["..."], "note": "..."}

with `kb` relative to the case file. The probe copies that knowledge base into a fresh directory, creates an
empty store beside it, and runs the phase under the case's run ID with the case's questions in place of the
production ones. The store is the probe's own, so no production store is written and no earlier run's papers
make the fetch unnecessary.

Exit status: 0 when at least one paper was fetched by an agent **and** at least one claim of the run is
`verified`, 1 when not, 2 when the case is not a valid delivery.
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

from knowledge_base.schemas import Claim
from knowledge_base.store import LOCK_FILE, read_records

from .config import DEFAULT_CONFIG, PipelineConfig, load_config
from .launcher import ClaudeLauncher, Launcher
from .orchestrator import REPO_ROOT, Orchestrator

DEFAULT_MAX_COST_USD = 3.0  # `literature` is the costliest phase per agent; one question keeps it to one agent
AGENT_FETCH = 4  # the budget this probe grants, enough for several attempts on one question


class InvalidDelivery(ValueError):
    """The case is not something `literature` could run."""


@dataclass(frozen=True)
class Case:
    name: str
    run_id: str
    kb: Path
    questions: tuple[str, ...]


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
        questions=tuple(data["questions"]),
    )


def check_case(case: Case) -> list[str]:
    """Every reason this is not a case `literature` can run; empty when it is."""
    reasons = []
    if not case.questions:
        reasons.append("the case names no question, so the phase would launch no agent")
    if not case.kb.is_dir():
        reasons.append(f"no knowledge base at {case.kb}")
    if not case.run_id.strip():
        reasons.append("the case names no run id")
    return reasons


def prepare(case: Case, out_root: Path, now: datetime) -> Probe:
    """A fresh probe directory with the case's knowledge base and an **empty** literature store.

    Raises `InvalidDelivery` before anything is written when the case could not run.
    """
    if reasons := check_case(case):
        raise InvalidDelivery("; ".join(reasons))
    root = out_root / f"probe-literature-{case.name}-{now.strftime('%Y%m%d-%H%M%S')}"
    run_dir, kb_root, store = root / "results", root / "kb", root / "literature"
    run_dir.mkdir(parents=True)  # fails if it exists: a probe never overwrites another
    store.mkdir()
    # Empty, and empty on purpose: with no paper in it the agents cannot verify a single quote without
    # fetching, so the probe measures the behaviour instead of hoping for it.
    (store / "index.jsonl").touch()
    shutil.copytree(case.kb, kb_root, ignore=shutil.ignore_patterns(LOCK_FILE))
    return Probe(case, run_dir, kb_root, store)


def probe_config(path: Path, case: Case, store: Path, max_cost_usd: float) -> PipelineConfig:
    """The production config, with this probe's questions, store, fetch budget and spend cap.

    The models and the prompt are **not** replaced: they are what is being tested.
    """
    config = load_config(path)
    literature = config.phases.literature.model_copy(
        update={"questions": list(case.questions), "agent_fetch_max_new": AGENT_FETCH}
    )
    return config.model_copy(
        update={
            "literature_store": store,
            "run_max_cost_usd": max_cost_usd,
            "phases": config.phases.model_copy(update={"literature": literature}),
        }
    )


def run(
    probe: Probe, config: PipelineConfig, launcher_factory: Callable[..., Launcher] = ClaudeLauncher, commit: str = ""
) -> dict[str, Any]:
    """Launch `literature` alone and return its report. Writes `probe.json` beside it, for provenance."""
    (probe.run_dir / "probe.json").write_text(
        json.dumps(
            {
                "case": probe.case.name,
                "phase": "literature",
                "run_id": probe.case.run_id,
                "kb": str(probe.kb_root),
                "store": str(probe.store),
                "questions": list(probe.case.questions),
                "agent_fetch_max_new": config.phases.literature.agent_fetch_max_new,
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
    return orchestrator.run(("literature",))["phases"]["literature"]


def readout(probe: Probe) -> dict[str, Any]:
    """What the run shows about the behaviour under test, from the archive alone.

    The phase report is read **from disk**, not from what `Orchestrator.run` returns: that return value is the
    summary written into `state.json`, with every list and map reduced to its length, so `fetched` arrives as
    an integer. Reading the artifact is also the project's own rule about never taking a run's state from
    anything but what it wrote.
    """
    reports = sorted(probe.run_dir.glob("*-literature-report.json"))
    report = json.loads(reports[-1].read_text()) if reports else {}
    audit = probe.run_dir / "literature-calls.agents.jsonl"
    rows = [json.loads(line) for line in audit.read_text().splitlines() if line.strip()] if audit.is_file() else []
    fetches = [r for r in rows if r.get("event") == "fetch"]
    checks = [r for r in rows if r.get("quote")]
    records = read_records(probe.kb_root)
    claims = [r for r in records.values() if isinstance(r, Claim) and r.run_id == probe.case.run_id]
    host = report.get("fetched") or {}
    return {
        "phase_report": reports[-1].name if reports else None,
        "agent_fetch_calls": len(fetches),
        "agent_fetch_outcomes": sorted({str(r.get("outcome")) for r in fetches}),
        "agent_papers_fetched": sum(1 for r in fetches if r.get("outcome") == "fetched"),
        "agent_fetch_retries": sum(int(r.get("retries") or 0) for r in fetches),
        "agent_quote_checks": len(checks),
        "agent_checks_exact": sum(1 for r in checks if r.get("verdict") == "exact"),
        "host_fetches": {k: v for k, v in host.items()},
        "claims_written": len(claims),
        "claims_verified": sum(1 for c in claims if c.provenance_status == "verified"),
        "papers_in_store_now": sum(1 for _ in (probe.store / "index.jsonl").read_text().splitlines() if _.strip()),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("case", type=Path, help="a case file, e.g. tests/fixtures/literature/cases/*.json")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--max-cost-usd", type=float, default=DEFAULT_MAX_COST_USD)
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "experiments")
    args = parser.parse_args(argv)
    case = load_case(args.case.resolve())
    try:
        probe = prepare(case, args.out.resolve(), datetime.now(timezone.utc))
    except InvalidDelivery as err:
        print(f"literature-probe: {case.name} is not a valid delivery: {err}", file=sys.stderr)
        return 2
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True).stdout.strip()
    print(
        f"literature-probe: {case.name}, run {case.run_id}, {len(case.questions)} question(s), PAID "
        f"(one literature session, cap {args.max_cost_usd} USD)",
        file=sys.stderr,
    )
    report = run(probe, probe_config(args.config, case, probe.store, args.max_cost_usd), commit=commit)
    out = readout(probe)
    print(
        json.dumps(
            {
                "case": case.name,
                **out,
                "cost_usd": (report.get("launch") or {}).get("session_cost_usd"),
                "results": str(probe.run_dir),
            },
            indent=2,
        )
    )
    # The chain the 85 dead ends never completed: the agent fetched a paper it had identified, and a claim of
    # this run rests on a quote verified against that paper's text.
    return 0 if out["agent_papers_fetched"] > 0 and out["claims_verified"] > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
