"""The host orchestrator: runs the phases in order and is the single writer of the KB.

For each phase: build the args (config + KB slice + generated JSON Schema), launch the phase workflow, validate
every agent output against its Pydantic model, check provenance, commit the records, write a phase report, and
checkpoint. A re-run with the same run directory skips the phases already checkpointed (resume at the phase
boundary). An agent output that fails validation is reported and skipped; it does not stop the phase.
"""

from __future__ import annotations

import copy
import json
import re
import sys
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from knowledge_base.chains import causal_chains
from knowledge_base.retrieve import as_context, by_id, neighbourhood
from knowledge_base.schemas import Claim, Decision, Experiment, Hypothesis, Ignorance, Intervention, Relation
from knowledge_base.status import mechanism_graph, statuses, superseded
from knowledge_base.store import KnowledgeBase
from tools.data.local_sources import Acquisition, LocalSource, manifest
from tools.data.pharmacology import lookup_match
from tools.literature.search import DEFAULT_CACHE as SEARCH_CACHE
from tools.literature.store import LiteratureStore, canonical_id

from . import prior_graph
from .compute import run_all, run_one
from .config import PACKAGE_ROOT, AcquireConfig, PipelineConfig
from .experiment_execution import check_interpretation, select_all
from .experiment_feasibility import assess
from .launcher import Launcher, PhaseFailed
from .proposals import (
    ArbitrateOutput,
    Builder,
    ClosestWork,
    DesignOutput,
    ExecuteOutput,
    GenerateOutput,
    InterveneOutput,
    LiteratureOutput,
    NoveltyOutput,
    ReassessOutput,
    ReviewOutput,
    SynthesizeOutput,
    Verifier,
    VerifyOutput,
    agent_schema,
    drop_invalid_claims,
)
from .provenance import QuoteVerifier, fetch_missing, fetch_outcome
from .synthesis import gate, normalize, render

# `PACKAGE_ROOT` is the repository root.
REPO_ROOT = PACKAGE_ROOT
PHASES = (
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
)
# Phases that run no agent: deterministic Python in this process, so no launch and no cost.
CODE_PHASES = ("data", "compute")
OUTPUT_MODELS: dict[str, type[BaseModel]] = {
    "literature": LiteratureOutput,
    "generate": GenerateOutput,
    "review": ReviewOutput,
    "verify": VerifyOutput,
    "design": DesignOutput,
    "arbitrate": ArbitrateOutput,
    "execute": ExecuteOutput,
    "reassess": ReassessOutput,
    "intervene": InterveneOutput,
    "synthesize": SynthesizeOutput,
}
# The stage run after the pipeline: by `pipeline.cli run` once `synthesize` has completed (unless `--no-novelty`), or later
# by `pipeline.cli novelty`. Not in `PHASES`, so a run is complete without it. Its step number follows the last phase, so its report does not take another phase's name.
NOVELTY_STEP = len(PHASES) + 1
# Phases with one item (and one agent) per hypothesis of the run.
PER_HYPOTHESIS = ("review", "design", "arbitrate", "intervene")
# Fields that would tell a reviewer who wrote a hypothesis or how sure the author was (reviews are blinded).


def _acquisition(declared: AcquireConfig | None) -> Acquisition | None:
    """The configuration's acquisition procedure, in the form the missing-source report prints."""
    if declared is None:
        return None
    return Acquisition(
        see=declared.see,
        steps=tuple(("run", step.run) if step.run else ("by hand", step.by_hand) for step in declared.steps),
    )


def _binding_failures(
    judgement: Any, selected: dict[str, Any], digest: str, result_claims: list[str], allowed: set[str]
) -> list[str]:
    """Why a reassessment does not bind to the result it judges, one named reason per condition.

    One sentence for four conditions told the reader that something had failed, not which: diagnosing
    an earlier run's refusal cost a replay to discover that the offending ID was `C-1139`. Each reason names
    the field and, where there is one, the value that fails.
    """
    reasons = []
    if judgement.experiment_id != selected["experiment_id"]:
        reasons.append(f"experiment_id {judgement.experiment_id} is not the executed {selected['experiment_id']}")
    if judgement.result_digest != digest:
        reasons.append("result_digest is not the digest of this execution")
    if outside := sorted(set(judgement.claim_ids) - allowed):
        reasons.append(f"claim_ids outside the claims this judgement was shown: {', '.join(outside)}")
    if not set(result_claims) & set(judgement.claim_ids):
        reasons.append(f"claim_ids cites none of the computed-result claims {', '.join(result_claims)}")
    return reasons


def _reuse_execution(chosen: dict[str, Any], runner: dict[str, Any]) -> dict[str, Any]:
    """An EXP whose computation another EXP of this run performed: bound to that result, writing nothing.

    The result is the runner's, with its digest, artifact and interpretation check, and so are its claims; the
    store gains no row and no reference, because nothing new was measured.
    """
    row = {key: chosen[key] for key in ("experiment_id", "hypothesis_id", "analysis_id", "tested_claims")}
    row["result_of"] = runner["experiment_id"]
    if runner.get("status") != "executed":
        return row | {
            "status": "not_executed",
            "reason": f"the EXP computing its result, {runner['experiment_id']}, did not execute",
        }
    return row | {
        "status": "executed",
        "result_digest": runner["result_digest"],
        "artifact": runner["artifact"],
        "interpretation_check": runner["interpretation_check"],
        "written": [],
        "seen_again": {},
        "result_claims": list(runner["result_claims"]),
    }


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _to_stderr(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


def _duration(seconds: float) -> str:
    minutes, secs = divmod(round(seconds), 60)
    return f"{minutes}m{secs:02d}s" if minutes else f"{secs}s"


def _search_cache_records(cache_path: Path) -> list[dict[str, Any]]:
    """Every answer the search tool has cached, from its append-only file; empty when there is none."""
    if not cache_path.is_file():
        return []
    return [json.loads(line) for line in cache_path.read_text().splitlines() if line.strip()]


def _papers_opened(server_log: Path, offset: int) -> set[str]:
    """The papers `read_paper` actually served, from this stage's own lines of the agents' server log."""
    if not server_log.is_file():
        return set()
    rows = [json.loads(line) for line in server_log.read_text().splitlines()[offset:] if line.strip()]
    return {r["paper"] for r in rows if r.get("event") == "read" and r.get("outcome") == "read" and r.get("paper")}


def _served_works(cache_path: Path) -> dict[str, str | None]:
    """Every OpenAlex work the search tool has ever returned, mapped to the paper id of its DOI: the only works
    an agent may cite, and the only way to tell whether one of them was read in full (the server log records the
    paper, never the work). Empty when the cache does not exist, so nothing is citable rather than everything.

    The scope is the whole cache, every run and stage, not this stage's own searches: a work served to an
    earlier attempt still passes. Narrowing it here would make every cache miss a dropped work;
    `evaluation.novelty_diagnosis` reports the per-stage view instead (`CITED_OUTSIDE_THIS_STAGE`).
    """
    served: dict[str, str | None] = {}
    for rec in _search_cache_records(cache_path):
        for work in rec.get("works", []):
            if not (wid := work.get("openalex_id")):
                continue
            paper = canonical_id(work.get("doi")) if work.get("doi") else None
            # The same work can be cached by several queries, one of them without a DOI: keep the first paper
            # id found, so a work does not lose its full-text credit to a thinner record.
            if served.get(wid) is None:
                served[wid] = paper
    return served


def _work_provenance(
    found: NoveltyOutput, served: Mapping[str, str | None], read_by_agent: set[str]
) -> tuple[dict[str, Any], list[ClosestWork]]:
    """What the host could check about the works an agent named, and the works that survive the check.

    Mechanical only: a work is kept when the search tool returned it, and marked `full_text` when the paper of
    its DOI is one *this agent* read — the papers it declared in `full_texts_read` that this stage's server log
    confirms. The caller computes that set, because `read_paper` logs the paper and never who asked for it: a
    stage-wide set of opened papers would credit one hypothesis with another agent's reading, which overstates
    how well grounded its verdict is. No verdict is revised here — a drop is a fact about provenance, never
    about the science.
    """
    kept = [w for w in found.closest_works if w.openalex_id in served]
    dropped = [
        {"openalex_id": w.openalex_id, "title": w.title, "reason": "the search tool never returned this work"}
        for w in found.closest_works
        if w.openalex_id not in served
    ]
    listed_bare = {w.openalex_id.rsplit("/", 1)[-1] for w in found.closest_works}
    served_bare = {wid.rsplit("/", 1)[-1] for wid in served}
    rationale_only, unlisted = [], sorted(set(re.findall(r"\bW\d{6,}\b", found.rationale)) - listed_bare)
    for wid in unlisted:
        if wid in served_bare:
            rationale_only.append(wid)
        else:
            dropped.append(
                {
                    "openalex_id": wid,
                    "title": None,
                    "reason": "named in the rationale only, and the search tool never returned it",
                }
            )
    provenance = {
        "kept": [
            {
                "openalex_id": w.openalex_id,
                "evidence_depth": "full_text" if served.get(w.openalex_id) in read_by_agent else "abstract",
            }
            for w in kept
        ],
        "dropped": dropped,
        "rationale_only": rationale_only,
        "works_listed": len(kept),
        "works_dropped": len(dropped),
    }
    provenance["works_read_in_full"] = sum(w["evidence_depth"] == "full_text" for w in provenance["kept"])
    return provenance, kept


def _queries_that_returned_works(cache_path: Path) -> set[str]:
    """The queries (case and spacing normalised) the search tool answered with at least one work. A query refused
    for budget is never cached, so it is not here."""
    return {
        " ".join(rec["query"].lower().split())
        for rec in _search_cache_records(cache_path)
        if rec.get("works") and isinstance(rec.get("query"), str)
    }


class Orchestrator:
    def __init__(
        self,
        config: PipelineConfig,
        kb_root: Path,
        run_dir: Path,
        run_id: str,
        launcher: Launcher,
        verifier: Verifier | None = None,
        clock: Callable[[], datetime] = _utc_now,
        fetch: Callable[..., dict[str, str]] = fetch_missing,
        config_change: str | None = None,
        compute_runner: Callable[..., Any] = run_one,
        progress: Callable[[str], None] = _to_stderr,
    ) -> None:
        # A resumed run must use the config it started with, unless the change is declared (and then recorded).
        self.config_change = config_change
        self.config, self.kb_root, self.run_dir, self.run_id = config, kb_root, run_dir, run_id
        self.launcher, self.clock, self.fetch = launcher, clock, fetch
        self.compute_runner = compute_runner  # injected so a unit test need not run a real analysis
        self.progress = progress  # one line per phase, so a long run is not silent
        self.store = LiteratureStore(config.store_path()) if verifier is None else None
        self.verifier = verifier or QuoteVerifier(self.store, run_dir / "literature-calls.jsonl")
        self.state_path = run_dir / "state.json"

    # --- state ------------------------------------------------------------------------------------------------

    def _state(self) -> dict[str, Any]:
        if self.state_path.is_file():
            state = json.loads(self.state_path.read_text())
            if state.get("run_id") != self.run_id:
                raise ValueError(f"{self.state_path} belongs to run {state.get('run_id')}, not {self.run_id}")
            if state.get("config_hash") != self.config.config_hash:
                if not self.config_change:
                    raise ValueError(
                        f"run {self.run_id} started with config {state.get('config_hash')}, now "
                        f"{self.config.config_hash}: resume with the same config, or declare the change"
                    )
                state.setdefault("config_changes", []).append(
                    {
                        "from": state.get("config_hash"),
                        "to": self.config.config_hash,
                        "reason": self.config_change,
                        "at_phase": len(state["completed"]) + 1,
                        "at": self.clock().isoformat(),
                    }
                )
                state["config_hash"] = self.config.config_hash
            return state
        return {"run_id": self.run_id, "config_hash": self.config.config_hash, "completed": [], "phases": {}}

    def _save(self, state: dict[str, Any]) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2))
        tmp.replace(self.state_path)

    # --- args per phase ---------------------------------------------------------------------------------------

    def _code_phase(self, phase: str, kb: KnowledgeBase) -> dict[str, Any]:
        """A phase with no agent. `data` resolves and hashes the sources the config declares, and writes the
        manifest the computed claims will point at. A missing required source raises: a run must not report less
        evidence than it had available without saying so."""
        if phase == "compute":
            return self._compute(kb)
        sources = [
            LocalSource(
                id=s.id,
                path=Path(s.path),
                kind=s.kind,
                license_note=s.license_note,
                required=s.required,
                acquire=_acquisition(s.acquire),
            )
            for s in self.config.phases.data.sources
        ]
        data = manifest(sources, REPO_ROOT)
        (self.run_dir / "data-manifest.json").write_text(json.dumps(data, indent=2) + "\n")
        return {
            "sources": {e["id"]: e["ref"] for e in data["sources"]},
            "absent": [a["id"] for a in data["absent"]],
            "bytes": data["bytes"],
            "files": data["files"],
        }

    def _prior(self, kb: KnowledgeBase) -> dict[str, Any]:
        """The prior causal graph of this run: a versioned selection over claims, written once after `literature`.

        Selecting which edges form the scaffold is a modelling choice, so it lives in an artifact that references
        claim IDs and owns no content; the claims themselves stay in the append-only knowledge base, where they
        can be superseded but never removed.
        """
        path = self.run_dir / "prior-causal-graph.json"
        if path.is_file():  # a resumed run keeps the prior it started from
            return json.loads(path.read_text())
        prior = prior_graph.build(kb.records, self.run_id, self.config.objective)
        path.write_text(json.dumps(prior, indent=2) + "\n")
        return prior

    def _snapshot_graph(self, kb: KnowledgeBase) -> None:
        """What causal structure this run inherited, written once before the first phase.

        The agents read the graph live through `mcp__kb__kb_mechanism_graph`, because the knowledge base grows
        during the run and a snapshot would be stale by the design phase. This file is the provenance record of
        the starting point: what the run was handed, as against what it added.
        """
        path = self.run_dir / "inherited-mechanism-graph.json"
        if path.exists():  # a resumed run keeps the graph it started from
            return
        edges = [vars(e) for e in mechanism_graph(kb.records)]
        path.write_text(
            json.dumps(
                {"run_id": self.run_id, "at": self.clock().isoformat(), "edges": edges, "total": len(edges)}, indent=2
            )
            + "\n"
        )

    def _declared_datasets(self) -> set[str] | None:
        """The source ids this run's data manifest holds, or None when the `data` phase did not run (then a
        design agent's dataset reference cannot be checked, and is kept as given)."""
        path = self.run_dir / "data-manifest.json"
        if not path.is_file():
            return None
        return {entry["id"] for entry in json.loads(path.read_text()).get("sources", [])}

    def _feasibility(self) -> dict[str, Any]:
        for path in sorted(self.run_dir.glob("*-design-feasibility.json")):
            return json.loads(path.read_text())
        return {"experiments": {}}

    def _write_feasibility(self, step: int, kb: KnowledgeBase, report: dict[str, Any]) -> None:
        manifest_path = self.run_dir / "data-manifest.json"
        manifest = json.loads(manifest_path.read_text()) if manifest_path.is_file() else None
        rows = {
            r.id: assess(r, manifest)
            for r in kb.records.values()
            if isinstance(r, Experiment) and r.run_id == self.run_id
        }
        refused = []
        for agent_id, reasons in report["dropped_elements"].items():
            for reason in reasons:
                title = reason.split(": ", 1)[0].removeprefix("experiment ").strip("'\"")
                refused.append(
                    {"agent_id": agent_id, "title": title, "experiment_id": None, "status": "refused", "reason": reason}
                )
        for agent_id, reason in report["invalid_outputs"].items():
            refused.append(
                {
                    "agent_id": agent_id,
                    "title": None,
                    "experiment_id": None,
                    "status": "invalid_output",
                    "reason": reason,
                }
            )
        document = {
            "run_id": self.run_id,
            "manifest": "data-manifest.json" if manifest else None,
            "experiments": rows,
            "refused": refused,
        }
        (self.run_dir / f"{step:02d}-design-feasibility.json").write_text(json.dumps(document, indent=2) + "\n")
        report["feasibility"] = {
            "data_supported": sum(r["status"] == "data_supported" for r in rows.values()),
            "missing_inputs": sum(r["status"] == "missing_inputs" for r in rows.values()),
            "unverified": sum(r["status"] == "unverified" for r in rows.values()),
        }

    def _compute(self, kb: KnowledgeBase) -> dict[str, Any]:
        """Run the configured analyses on the manifest's paths and commit the claims their verified artifacts
        license. The analyses are deterministic, so a second run of the same code on the same inputs produces the
        same claims, which the KB records as `seen_again` rather than as new evidence."""
        manifest_path = self.run_dir / "data-manifest.json"
        if not manifest_path.is_file():
            raise PhaseFailed("compute: the data phase must run first (no data-manifest.json)")
        settings = self.config.phases.compute
        claims, report = run_all(
            settings.analyses,
            json.loads(manifest_path.read_text()),
            REPO_ROOT,
            self.run_dir / "analysis",
            settings.seed,
            self.run_id,
            self.config.config_hash,
            runner=self.compute_runner,
        )
        builder = Builder(kb.records, kb.allocate, self.run_id, "compute", self.clock())
        for claim in claims:
            builder.computed(claim.content, claim.type, claim.source_refs)
        result = kb.commit(builder.batch.records, run_id=self.run_id, agent_id="compute")
        return {
            "analyses": report,
            "written": list(result.written),
            "seen_again": dict(result.seen_again),
            "rejected": dict(result.rejected),
            "claims": len(claims),
        }

    def _args(self, phase: str, kb: KnowledgeBase) -> dict[str, Any]:
        settings = getattr(self.config.phases, phase)
        args: dict[str, Any] = {
            "phase": phase,
            "run_id": self.run_id,
            "objective": self.config.objective,
            "schema": agent_schema(OUTPUT_MODELS[phase]),
            "model": settings.model,
            "web": self.config.web_budget(phase).model_dump(),
            # The same number the launcher passes the literature server as `--max-fetch`,
            # so the prompt describes the tool exactly when the tool exists.
            "agent_fetch": self.config.agent_fetch_budget(phase),
        }
        if phase == "literature":
            # No slice on purpose: extraction from a paper should not be steered by what the store already holds,
            # and the four agents reach the store through the KB tools when they want to check for a duplicate.
            # `contextBlock` says exactly that; it must not claim the knowledge base is empty (it is not).
            args |= {"questions": list(settings.questions), "max_claims": settings.max_claims, "kb_context": []}
        elif phase == "generate":
            args |= {
                "perspectives": list(settings.perspectives),
                "kb_context": self._claims_context(kb, settings.context_limit, self.run_id),
                "prior_graph": prior_graph.for_prompt(self._prior(kb)),
            }
        elif phase == "review":
            args |= {"reviewers": settings.reviewers, "items": self._review_items(kb, settings.hops)}
        elif phase in ("design", "arbitrate"):
            args |= {"items": self._review_items(kb, settings.hops, open_only=True)}
            if phase == "arbitrate":
                rows = self._feasibility()["experiments"]
                for item in args["items"]:
                    item["experiment_feasibility"] = {
                        eid: row for eid, row in rows.items() if row["hypothesis_id"] == item["hypothesis_id"]
                    }
        elif phase == "intervene":
            args |= {"items": self._surviving_items(kb, settings.hops)}
            args["execution"] = self._read_report("execute") or {"status": "not_executed"}
            args["reassessment"] = self._read_report("reassess") or {"status": "not_reassessed"}
        elif phase == "synthesize":
            items = self._review_items(kb, settings.hops)
            args |= {"items": [{"hypotheses": items, "decisions": self._decisions(kb)}] if items else []}
            args["experiment_feasibility"] = self._feasibility()["experiments"]
            args["execution"] = self._read_report("execute") or {"status": "not_executed"}
            args["reassessment"] = self._read_report("reassess") or {"status": "not_reassessed"}
        elif phase == "verify":
            candidates = self._verify_candidates(kb, only_own=settings.only_on_run_kb)
            size = settings.batch_size
            args |= {"batches": [candidates[i : i + size] for i in range(0, len(candidates), size)]}
        # The session-wide search cap of the CLI: the per-agent budget times the agents this phase launches.
        agents = {
            "literature": len(args.get("questions", [])),
            "generate": len(args.get("perspectives", [])),
            "review": len(args.get("items", [])) * args.get("reviewers", 0),
            "verify": len(args.get("batches", [])),
            "design": len(args.get("items", [])),
            "arbitrate": len(args.get("items", [])),
            "intervene": len(args.get("items", [])),
            "synthesize": len(args.get("items", [])),
        }[phase]
        args["web"]["session_searches"] = args["web"]["searches"] * agents
        return args

    def _decisions(self, kb: KnowledgeBase) -> list[dict[str, Any]]:
        return [
            r.model_dump(exclude={"created_at", "run_id", "agent_id"})
            for r in kb.records.values()
            if isinstance(r, Decision) and r.run_id == self.run_id
        ]

    def _review_items(self, kb: KnowledgeBase, hops: int, open_only: bool = False) -> list[dict[str, Any]]:
        """The current hypotheses of this run, each with its k-hop neighbourhood, author fields removed, and the
        targets an experiment should aim at (its unresolved or contested claims and its declared gaps).
        `open_only` leaves out hypotheses already decided by the arbiter in this run."""
        records = kb.records
        old = superseded(records)
        status = statuses(records)
        decided = {r.hypothesis_id for r in records.values() if isinstance(r, Decision) and r.run_id == self.run_id}
        items = []
        for h in records.values():
            if (
                isinstance(h, Hypothesis)
                and h.run_id == self.run_id
                and h.id not in old
                and not (open_only and h.id in decided)
            ):
                # The author fields are removed by `as_context` itself, for the pushed slice and the pulled
                # tools alike (`knowledge_base/retrieve.py`: BLINDED).
                context = as_context(neighbourhood([h.id], records, hops), records)
                parts = [
                    r.source_id
                    for r in records.values()
                    if isinstance(r, Relation) and r.type == "part_of" and r.target_id == h.id
                ]
                targets = [c for c in parts if status.get(c) in ("unresolved", "contested")]
                gaps = [r.id for r in records.values() if isinstance(r, Ignorance) and r.hypothesis_id == h.id]
                items.append({"hypothesis_id": h.id, "context": context, "targets": targets, "gaps": gaps})
        return items

    def _surviving_items(self, kb: KnowledgeBase, hops: int) -> list[dict[str, Any]]:
        """The hypotheses of this run the arbiter did not reject. A hypothesis the arbiter never reached is not a
        survivor: a candidate must rest on a hypothesis that was judged."""
        kept = {
            r.hypothesis_id
            for r in kb.records.values()
            if isinstance(r, Decision) and r.run_id == self.run_id and r.decision != "rejected"
        }
        return [item for item in self._review_items(kb, hops) if item["hypothesis_id"] in kept]

    def _read_report(self, phase: str) -> dict[str, Any] | None:
        """A finished phase's report, by phase name: the step number is not known here and a re-attempt does not
        change the file. `None` when the phase did not run, which the caller must treat as "no information"."""
        for path in sorted(self.run_dir.glob(f"*-{phase}-report.json")):
            try:
                return json.loads(path.read_text())
            except (OSError, ValueError):
                return None
        return None

    def _verify_candidates(self, kb: KnowledgeBase, only_own: bool = False) -> list[dict[str, Any]]:
        """Agent-inferred supports/contradicts that no verified relation already covers and whose source claim is
        current and **has something to check**: either already `verified`, or carrying a quote.

        `only_own` (config `phases.verify.only_on_run_kb`, default and recommendation `False`) keeps
        only the relations **this run wrote**. The pool is otherwise the whole store, which is what makes an
        append-only knowledge base worth keeping — a relation one run asserts, a later run can judge — and also
        what makes the phase the most expensive one: most of what `verify` judges, and pays for, is other runs'
        work. `True` buys that back and gives up the cross-run judgement with it, so it belongs in a run whose
        pre-registration says why.

        The second half is the point: a claim that quotes a source but is unverified would otherwise never be
        offered, so the verifier's `faithful` question — "does this claim say what its quote says, and no more?" —
        would never be asked about it, and citations that overstate their source are typically of this shape.

        What it buys, and what it does not: a judgement where there was none, and the narrowing path, which
        writes a *new* verified claim from the verifier's restatement and whose relation **is** active. A merely
        faithful confirmation on one of these leaves the relation inert, because `status.is_active` requires the
        source claim to be verified and it still is not. The widening therefore adds judgements, narrowings and
        warnings, never new verified support.
        """
        records = kb.records
        old = superseded(records)
        # A verified relation asserted from a claim narrowed out of another one (`Builder.narrowed`) covers the
        # relation it came from: without this the original is offered again on every later run, and each
        # differently worded restatement would add another verified claim saying the same thing.
        parent = {
            r.source_id: r.target_id
            for r in records.values()
            if isinstance(r, Relation) and r.type == "derived_from" and r.provenance_status == "verified"
        }
        covered = {
            (parent.get(r.source_id, r.source_id), r.target_id, r.type)
            for r in records.values()
            if isinstance(r, Relation) and r.provenance_status == "verified"
        }
        out = []
        for r in records.values():
            if not (
                isinstance(r, Relation)
                and r.type in ("supports", "contradicts")
                and r.justification_method == "agent_inference"
                and (r.source_id, r.target_id, r.type) not in covered
            ):
                continue
            if only_own and r.run_id != self.run_id:
                continue
            source, target = records.get(r.source_id), records.get(r.target_id)
            checkable = isinstance(source, Claim) and (source.provenance_status == "verified" or bool(source.quote))
            if checkable and source.id not in old and isinstance(target, Claim):
                covered.add((r.source_id, r.target_id, r.type))
                out.append(
                    {
                        "relation_id": r.id,
                        "type": r.type,
                        "source": {
                            "id": source.id,
                            "content": source.content,
                            "quote": source.quote,
                            "source_refs": [x for x in source.source_refs if not x.startswith("receipt:")],
                        },
                        "target": {"id": target.id, "content": target.content},
                    }
                )
        return out

    COMPUTED_SHARE = 4  # at most a quarter of the slice, so computed claims never crowd out the literature
    FRESH_SHARE = 3  # at most a third for this run's own claims, which nothing else in the slice carries

    @staticmethod
    def _claims_context(kb: KnowledgeBase, limit: int, run_id: str = "") -> list[dict[str, Any]]:
        """Current claims for the hypothesis authors: reserved shares for computed and for this run's own
        evidence, then verified first.

        A sort alone cannot reach a claim that accumulation has pushed past `limit`:

        - without `COMPUTED_SHARE`, the first `limit` verified claims are all `origin: literature` and no
          `origin: computation` claim reaches the slice;
        - without `FRESH_SHARE`, Python's stable sort keeps append order inside the verified block, i.e. oldest
          first, so once the store holds more verified claims than `context_limit` the claims this run's own
          `literature` phase just wrote are unreachable by construction.

        So a reserved share again, mirroring the computed one: this run's claims, verified first and newest
        first within that. It bounds the reservation instead of re-sorting the whole slice, which leaves the
        accumulated knowledge base the majority of the places.
        """
        records = kb.records
        old = superseded(records)
        claims = [r for r in records.values() if isinstance(r, Claim) and r.id not in old]
        computed = [c for c in claims if c.origin == "computation"][: max(1, limit // Orchestrator.COMPUTED_SHARE)]
        taken = {c.id for c in computed}
        fresh = [c for c in claims if c.id not in taken and run_id and c.run_id == run_id]
        fresh.sort(key=lambda c: c.created_at, reverse=True)  # newest first ...
        fresh.sort(key=lambda c: c.provenance_status != "verified")  # ... verified first, stably
        fresh = fresh[: max(0, limit // Orchestrator.FRESH_SHARE)]
        taken |= {c.id for c in fresh}
        rest = [c for c in claims if c.id not in taken]
        rest.sort(key=lambda c: c.provenance_status != "verified")
        chosen = [*computed, *fresh, *rest[: max(0, limit - len(computed) - len(fresh))]]
        # `by_id` answers in store order, which is append order, i.e. oldest first: it would undo the choice
        # above, and position is the only freshness signal an agent has (the context carries no date).
        selected = by_id([c.id for c in chosen], records)
        return as_context({c.id: selected[c.id] for c in chosen if c.id in selected}, records)

    # --- run ----------------------------------------------------------------------------------------------------

    def run(self, phases: tuple[str, ...] = PHASES) -> dict[str, Any]:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        state = self._state()
        with KnowledgeBase.open(self.kb_root, clock=self.clock) as kb:
            self._snapshot_graph(kb)
            for step, phase in enumerate(phases, 1):
                label = f"{step}/{len(phases)} {phase}"
                if phase in state["completed"]:
                    self.progress(f"pipeline: ✓ {label} (already completed)")
                    continue
                self.progress(f"pipeline: … {label} running")
                started = time.monotonic()
                try:
                    state["phases"][phase] = self._phase(phase, step, kb)
                except BaseException:
                    self.progress(f"pipeline: ✗ {label} stopped after {_duration(time.monotonic() - started)}")
                    raise
                state["completed"].append(phase)
                self._save(state)
                self.progress(f"pipeline: ✓ {label} done in {_duration(time.monotonic() - started)}")
        return state

    # --- novelty: after the pipeline -------------------------------------------------------------------------

    def archive_novelty_attempt(self) -> Path | None:
        """Move a finished novelty stage's files into `novelty-attempt-<n>/` and reopen the stage, so that a second
        attempt starts clean and the first stays in the archive. Nothing is deleted or overwritten; None when there
        is nothing to move."""
        mine = [p for p in self.run_dir.glob("13-novelty*")] + list((self.run_dir / "workflows").glob("13-novelty*"))
        if not mine:
            return None
        n = 1
        while (self.run_dir / f"novelty-attempt-{n}").exists():
            n += 1
        target = self.run_dir / f"novelty-attempt-{n}"
        (target / "workflows").mkdir(parents=True)
        for path in mine:
            path.rename(target / "workflows" / path.name if path.parent.name == "workflows" else target / path.name)
        state = self._state()
        if "novelty" in state["completed"]:
            state["completed"].remove("novelty")
            state["phases"].pop("novelty", None)
            self._save(state)
        return target

    def run_novelty(self, search_cache: Path | None = None) -> dict[str, Any]:
        """Look for prior art for each live hypothesis of the finished run, and write `13-novelty-report.json`.

        Not a phase of `PHASES`: a run is complete without it, and it is launched by its own command. It reads
        the knowledge base and writes nothing to it. The verdict is an agent's reading of OpenAlex abstracts, so
        every work it names is checked against the answers the search tool actually returned (`search_cache`):
        a work the host never served is a work the agent produced, so it is dropped from the entry with its
        reason, and the entry keeps its verdict. The entry is `unverified` only when a verdict of prior art is
        left with no served work at all. Each kept work also says whether this hypothesis's own agent read it
        in full, or only its abstract.
        """
        self.run_dir.mkdir(parents=True, exist_ok=True)
        state = self._state()
        if "novelty" in state["completed"]:
            return state["phases"]["novelty"]
        with KnowledgeBase.open(self.kb_root, clock=self.clock) as kb:
            report = self._novelty_phase(NOVELTY_STEP, kb, search_cache or PACKAGE_ROOT / SEARCH_CACHE)
        state["phases"]["novelty"] = report
        state["completed"].append("novelty")
        self._save(state)
        return report

    def _novelty_phase(self, step: int, kb: KnowledgeBase, search_cache: Path) -> dict[str, Any]:
        path = self.run_dir / f"{step:02d}-novelty-report.json"
        items = self._review_items(kb, self.config.phases.novelty.hops)
        if not items:
            report = {"status": "not_assessed", "reason": "no live hypothesis in this run"}
            path.write_text(json.dumps(report, indent=2) + "\n")
            return report
        args = {
            "phase": "novelty",
            "run_id": self.run_id,
            "objective": self.config.objective,
            "schema": agent_schema(NoveltyOutput),
            "model": self.config.phases.novelty.model,
            "web": {"searches": 0, "fetches": 0, "session_searches": 0},
            "max_papers_read": self.config.phases.novelty.max_papers_read,
            "items": [{"hypothesis_id": item["hypothesis_id"], "context": item["context"]} for item in items],
        }
        # The agents' server log is shared by every phase of the run and a second attempt appends to it, so the
        # stage records where its own lines start (`evaluation.novelty_diagnosis` reads from there).
        server_log = self.run_dir / "literature-calls.agents.jsonl"
        offset = len(server_log.read_text().splitlines()) if server_log.is_file() else 0
        outcome = self.launcher.run("novelty", step, args, self.run_dir)
        served, answered = _served_works(search_cache), _queries_that_returned_works(search_cache)
        opened = _papers_opened(server_log, offset)
        report: dict[str, Any] = {
            "status": "unverified",
            "entries": [],
            "assessed_count": 0,
            "sources_searched": ["openalex"],
            "searched_at": self.clock().isoformat(),
            "server_log_offset": offset,
            "launch": outcome.launch,
            "dropped_agents": outcome.result.get("dropped", []),
            "agent_outputs": outcome.result.get("outputs", []),
        }
        by_agent = {item.get("agent_id"): item.get("output") for item in report["agent_outputs"]}
        for item in items:
            hid = item["hypothesis_id"]
            entry: dict[str, Any] = {"hypothesis_id": hid, "status": "unverified"}
            output = by_agent.get(f"novelty@{hid}")
            if output is None:
                entry["error"] = f"no novelty output for {hid}"
            else:
                try:
                    found = NoveltyOutput.model_validate(output)
                    if found.hypothesis_id != hid:
                        raise ValueError(f"the output is about {found.hypothesis_id}, not {hid}")
                    # The papers this agent says it opened, in the identifier form the server's log uses. They
                    # are resolved here, before the provenance block, because the depth a work's evidence rests
                    # on is a per-agent fact while the log carries no agent id: intersected with the papers
                    # this stage's log confirms, the mark stays host-checked and an agent cannot inflate it,
                    # and the only error it can make is to under-count (a paper read but not declared reads as
                    # `abstract`). For an entry that survives, the check below leaves `claimed - opened` empty,
                    # so the intersection bites only on the block of an entry about to be refused — which is
                    # written anyway.
                    claimed = {canonical_id(d) or d for d in found.full_texts_read}
                    # Provenance is per work, not per answer: a work the tool never returned is dropped with
                    # its reason and the rest of the assessment stands. The block is attached before any
                    # further raise, so a refused entry still shows what was dropped.
                    provenance, kept = _work_provenance(found, served, claimed & opened)
                    entry["provenance"] = provenance
                    # The one case where nothing checkable is left. The diagnosis reads this exact triple
                    # (not assessed, no work kept, works dropped) as `VERDICT_ON_NO_SERVED_WORK`, so this
                    # check must stay before the two below.
                    if not kept and found.verdict != "no_prior_art_found":
                        raise ValueError(
                            "a verdict of prior art left with no work: the search tool never "
                            "returned " + ", ".join(w["openalex_id"] for w in provenance["dropped"])
                        )
                    # "Found nothing" is a statement about searches that were made: with none that returned a
                    # work, it says nothing (an agent refused for budget read zero abstracts).
                    if found.verdict == "no_prior_art_found" and not any(
                        " ".join(q.lower().split()) in answered for q in found.queries
                    ):
                        raise ValueError("no_prior_art_found rests on no search that returned a work")
                    # A full text the agent says it read must be one the server served: reading is the one
                    # claim here the host can check against a log, so it does.
                    if unserved := sorted(claimed - opened):
                        raise ValueError(
                            "full texts claimed as read but never served by read_paper: " + ", ".join(unserved)
                        )
                    entry.update(
                        status="assessed",
                        novelty=found.model_dump() | {"closest_works": [w.model_dump() for w in kept]},
                    )
                except (ValidationError, ValueError) as err:
                    entry["error"] = str(err)
            report["entries"].append(entry)
        report["assessed_count"] = sum(entry["status"] == "assessed" for entry in report["entries"])
        report["status"] = "assessed" if report["assessed_count"] else "unverified"
        path.write_text(json.dumps(report, indent=2) + "\n")
        return report

    def _phase(self, phase: str, step: int, kb: KnowledgeBase) -> dict[str, Any]:
        if phase in CODE_PHASES:
            report = self._code_phase(phase, kb)
            (self.run_dir / f"{step:02d}-{phase}-report.json").write_text(json.dumps(report, indent=2))
            return report
        if phase == "execute":
            return self._execute_phase(step, kb)
        if phase == "reassess":
            return self._reassess_phase(step, kb)
        args = self._args(phase, kb)
        if (phase in (*PER_HYPOTHESIS, "synthesize") and not args["items"]) or (
            phase == "verify" and not args["batches"]
        ):
            # Nothing to do: no launch, no cost; said in the report, not hidden.
            what = {"verify": "candidate relations", "intervene": "surviving hypotheses"}.get(phase, "open hypotheses")
            report = {"skipped": f"no {what} for this phase"}
            (self.run_dir / f"{step:02d}-{phase}-report.json").write_text(json.dumps(report, indent=2))
            return report
        outcome = self.launcher.run(phase, step, args, self.run_dir)
        report: dict[str, Any] = {
            "launch": outcome.launch,
            "dropped_agents": outcome.result.get("dropped", []),
            "invalid_outputs": {},
            "dropped_elements": {},
            "converted_elements": {},
            "unresolved_sources": {},
            "written": [],
            "seen_again": {},
            "rejected": {},
            "fetched": {},
        }
        if phase == "review":
            report["summaries"] = {}
        if phase == "verify":
            report["verdicts"] = {}
            report["not_narrowed"] = {}
        model = OUTPUT_MODELS[phase]
        for item in outcome.result.get("outputs", []):
            agent_id = str(item.get("agent_id"))
            try:
                raw, dropped, repaired = drop_invalid_claims(item.get("output"))
                if dropped:
                    report["dropped_elements"].setdefault(agent_id, []).extend(dropped)
                if repaired:
                    report["converted_elements"].setdefault(agent_id, []).extend(repaired)
                output = model.model_validate(raw)
                if phase in ("literature", "review") and self.store is not None:
                    sources = [c.source for c in output.claims if c.source]
                    # `fetch_max_new` caps the papers downloaded in the whole phase, not per agent output.
                    # `fetched` now holds {outcome, reason} per identifier; an archive from before this change
                    # holds the bare outcome string, and the budget must count the same either way.
                    left = self.config.fetch_max_new - sum(
                        fetch_outcome(v)[0] == "fetched" for v in report["fetched"].values()
                    )
                    report["fetched"] |= self.fetch(sources, self.store, self.run_id, max(left, 0))
                batches = self._batches(phase, output, item, agent_id, kb, report)
            except (ValidationError, ValueError) as err:
                report["invalid_outputs"][agent_id] = str(err)
                continue
            for batch in batches:
                if batch.skipped:
                    report["dropped_elements"].setdefault(agent_id, []).extend(batch.skipped)
                if batch.converted:
                    report["converted_elements"].setdefault(agent_id, []).extend(batch.converted)
                result = kb.commit(batch.records, self.run_id, agent_id)
                report["written"] += result.written
                report["seen_again"] |= result.seen_again
                report["rejected"] |= result.rejected
                # Resolved after the commit, not before it: the ID the builder allocated is dropped when the
                # store already holds that claim, so the report must name the ID a reader can actually look up.
                for entry in batch.unresolved:
                    stored = result.seen_again.get(entry["claim_id"], entry["claim_id"])
                    if stored in result.rejected:
                        continue
                    report["unresolved_sources"].setdefault(agent_id, []).append(
                        entry | {"claim_id": stored, "allocated_id": entry["claim_id"]}
                    )
        if phase == "literature":
            prior = self._prior(kb)
            report["prior_graph"] = {"ref": prior["ref"], "edges": prior["total"]}
        if phase == "design":
            self._write_feasibility(step, kb, report)
        if phase == "intervene" and self.config.phases.intervene.drug_lookup_enabled:
            self._drug_lookup(kb, report)
        (self.run_dir / f"{step:02d}-{phase}-report.json").write_text(json.dumps(report, indent=2))
        kept = (
            "launch",
            "proposal",
            "decisions",
            "no_experiment",
            "candidates",
            "no_candidate",
            "prior_graph",
            "feasibility",
        )  # small and worth reading in state.json
        return {k: (len(v) if isinstance(v, (list, dict)) and k not in kept else v) for k, v in report.items()}

    def _drug_lookup(self, kb: KnowledgeBase, report: dict[str, Any]) -> None:
        """Look up, host-side, every compound name `intervene` proposed this run and commit what the lookup says.

        Deterministic code, not an agent tool (the agent cannot version or control what it queries). One `Claim`
        per unique name, worded separately for found / no record / ambiguous, plus a `depends_on` relation from
        each `Intervention` naming it. The claim goes first in the batch so a dedup redirect rewrites the
        relation's target. A failed lookup (network, malformed answer) commits nothing and is reported, so a
        missing result is never read as "no record"."""
        named: dict[str, tuple[str, list[str]]] = {}
        for record in kb.records.values():
            if isinstance(record, Intervention) and record.run_id == self.run_id and record.drug_name:
                key = " ".join(record.drug_name.lower().split())
                named.setdefault(key, (record.drug_name, []))[1].append(record.id)
        lookups: dict[str, Any] = report.setdefault("drug_lookup", {})
        builder = Builder(kb.records, kb.allocate, self.run_id, "drug-lookup", self.clock())
        for key, (name, interventions) in sorted(named.items()):
            try:
                match, found, url = lookup_match(name, cache_path=self.config.phases.intervene.drug_lookup_cache)
            except Exception as err:  # reported below; a lookup that did not happen is not a result
                lookups[name] = {"error": f"{type(err).__name__}: {err}"}
                continue
            if match == "found":
                ids = {
                    "DrugBank": found.drugbank_id,
                    "ChEMBL": found.chembl_id,
                    "ChEBI": found.chebi_id,
                    "PubChem CID": found.pubchem_cid,
                    "UNII": found.unii,
                }
                listed = ", ".join(f"{k} {v}" for k, v in ids.items() if v)
                content = (
                    f"MyChem.info returned exactly one record matching the compound name '{name}': "
                    f"{listed or 'no external identifier'}."
                )
            elif match == "none":
                content = (
                    f"MyChem.info returned no record for the compound name '{name}'. This lookup does not "
                    f"confirm the name as a known compound; it does not show that no such compound exists."
                )
            else:
                content = (
                    f"MyChem.info returned several records for the compound name '{name}' and none "
                    f"matches it uniquely, so the compound's identity is unresolved by this lookup."
                )
            cid = builder.computed(content, "observational", [url])
            for iid in interventions:
                builder.batch.records.append(
                    Relation(
                        id=kb.allocate("relation"),
                        source_id=iid,
                        target_id=cid,
                        type="depends_on",
                        justification_method="code",
                        provenance_status="verified",
                        source_refs=[url],
                        run_id=self.run_id,
                        created_at=self.clock(),
                    )
                )
            lookups[name] = {"match": match, "claim_id": cid, "interventions": interventions}
        if builder.batch.records:
            result = kb.commit(builder.batch.records, self.run_id, "drug-lookup")
            report["written"] += list(result.written)
            report["seen_again"] |= dict(result.seen_again)
            report["rejected"] |= dict(result.rejected)
            # The ID the store kept, not the one proposed: a claim the store already held comes back under its
            # own ID, and a report naming the dropped one is the defect the rule prevents.
            for row in lookups.values():
                if "claim_id" in row:
                    row["claim_id"] = dict(result.seen_again).get(row["claim_id"], row["claim_id"])

    def _execute_phase(self, step: int, kb: KnowledgeBase) -> dict[str, Any]:
        """Every registered bought EXP, each invoked by its own narrow agent and verified by the host.

        One session launches one agent per selected EXP (`workflows/phases/src/execute.js`), because the MCP
        policy that authorises the tool is registered per stage, not per agent (`pipeline/launcher.py`). So the
        policy names the whole set and the host binds each artifact to its EXP afterwards.
        """
        selected, skipped = select_all(
            kb.records, self.run_id, self._feasibility()["experiments"], self.config.phases.execute.max_experiments
        )
        report: dict[str, Any] = {
            "selected": selected,
            "skipped": skipped,
            "executions": [],
            "executed_count": 0,
            "started_at": self.clock().isoformat(),
        }
        report_path = self.run_dir / f"{step:02d}-execute-report.json"
        if not selected:
            report.update(
                status="not_executed",
                reason="no bought EXP has a registered handler and supported inputs",
                finished_at=self.clock().isoformat(),
            )
            report_path.write_text(json.dumps(report, indent=2) + "\n")
            return report

        # Only a computation reaches the agents: a reuse names the EXP that computes it, launches no executor,
        # and is bound by the host to that EXP's verified result below.
        computed = [chosen for chosen in selected if not chosen.get("result_of")]
        policy = {
            "run_id": self.run_id,
            "config_hash": self.config.config_hash,
            "repository_root": str(REPO_ROOT),
            "selected": computed,
        }
        policy_path = self.run_dir / "execution-policy.json"
        if policy_path.exists() and json.loads(policy_path.read_text()) != policy:
            raise PhaseFailed("execute: existing policy differs from this run's selected EXPs")
        policy_path.write_text(json.dumps(policy, indent=2) + "\n")
        args = {
            "phase": "execute",
            "run_id": self.run_id,
            "objective": self.config.objective,
            "schema": agent_schema(ExecuteOutput),
            "model": self.config.phases.execute.model,
            "web": {"searches": 0, "fetches": 0, "session_searches": 0},
            "selected": computed,
        }
        outcome = self.launcher.run("execute", step, args, self.run_dir)
        report["launch"] = outcome.launch
        report["agent_outputs"] = outcome.result.get("outputs", [])
        report["dropped_agents"] = outcome.result.get("dropped", [])
        # The label routes an output to its EXP and the host wrote it, so an agent cannot claim another's
        # artifact by filling in a different `experiment_id`; `check_interpretation` then binds the output's
        # own fields to that artifact.
        by_agent = {item.get("agent_id"): item.get("output") for item in report["agent_outputs"]}
        rows = {
            chosen["experiment_id"]: self._verify_execution(
                chosen, by_agent.get(f"executor@{chosen['experiment_id']}"), kb
            )
            for chosen in computed
        }
        for chosen in selected:
            report["executions"].append(
                rows.get(chosen["experiment_id"]) or _reuse_execution(chosen, rows[chosen["result_of"]])
            )
        # Distinct computations, not EXPs: a result shared by three hypotheses was measured once.
        report["executed_count"] = sum(
            row["status"] == "executed" and not row.get("result_of") for row in report["executions"]
        )
        report["reused_count"] = sum(
            row["status"] == "executed" and bool(row.get("result_of")) for row in report["executions"]
        )
        report["status"] = "executed" if report["executed_count"] else "not_executed"
        if report["status"] == "not_executed":
            report["reason"] = "no selected EXP produced a verified artifact"
        report["finished_at"] = self.clock().isoformat()
        report_path.write_text(json.dumps(report, indent=2) + "\n")
        return report

    def _verify_execution(self, chosen: dict[str, Any], output: Any, kb: KnowledgeBase) -> dict[str, Any]:
        """One selected EXP: its artifact, its receipt, the agent's interpretation and its computed claims.

        One EXP's failure never stops another's — a missing artifact or a missing agent output is recorded
        against that EXP and the loop goes on. A verified artifact that belongs to a *different* EXP still
        raises, because that is the host's own binding failing rather than an agent's output.
        """
        from analysis.verify import verify_receipt

        from .compute import claims_of

        eid = chosen["experiment_id"]
        row: dict[str, Any] = {
            key: chosen[key] for key in ("experiment_id", "hypothesis_id", "analysis_id", "tested_claims")
        }
        artifact_dir = self.run_dir / "experiment-analysis" / eid
        summary_path = artifact_dir / "summary.json"
        if not summary_path.is_file():
            return row | {"status": "not_executed", "reason": "the computational agent did not produce an artifact"}
        verify_receipt(artifact_dir / "verification.json")
        summary = json.loads(summary_path.read_text())
        if summary.get("experiment_id") != eid or summary.get("analysis_id") != chosen["analysis_id"]:
            raise PhaseFailed(f"execute: the verified artifact of {eid} belongs to a different EXP or analysis")
        row |= {
            "status": "executed",
            "result_digest": summary["result_digest"],
            "artifact": f"experiment-analysis/{eid}/summary.json",
        }
        if output is None:
            row["interpretation_check"] = {"faithful": False, "errors": [f"no computational agent output for {eid}"]}
        else:
            try:
                row["interpretation_check"] = check_interpretation(
                    ExecuteOutput.model_validate(output), chosen, artifact_dir
                )
            except (ValidationError, ValueError, KeyError) as err:
                row["interpretation_check"] = {"faithful": False, "errors": [str(err)]}
        dataset_refs = [
            entry["ref"]
            for entry in json.loads((self.run_dir / "data-manifest.json").read_text())["sources"]
            if entry["id"] in {ref.split(":", 1)[-1].split("@", 1)[0] for ref in chosen["dataset_refs"]}
        ]
        facts = claims_of(
            chosen["analysis_id"], summary, [*dataset_refs, f"experiment:{eid}@{summary['result_digest'][:12]}"]
        )
        builder = Builder(kb.records, kb.allocate, self.run_id, "execute", self.clock())
        for fact in facts:
            builder.computed(fact.content, fact.type, fact.source_refs)
        committed = kb.commit(builder.batch.records, self.run_id, "execute")
        return row | {
            "written": list(committed.written),
            "seen_again": dict(committed.seen_again),
            "result_claims": [*committed.written, *sorted(committed.seen_again.values())],
        }

    def _reassess_phase(self, step: int, kb: KnowledgeBase) -> dict[str, Any]:
        """One independent judgement per executed EXP, each bound to its own result and its own hypothesis.

        Two EXPs of the same run can test the same hypothesis (an earlier run: H-1 bought both `EXP-1` and
        `EXP-2`), so each judgement is keyed by EXP and each gets its own copy of the hypothesis slice.
        """
        execution = self._read_report("execute") or {}
        path = self.run_dir / f"{step:02d}-reassess-report.json"
        executed = [row for row in execution.get("executions", []) if row.get("status") == "executed"]
        if execution.get("status") != "executed" or not executed:
            report = {"status": "not_reassessed", "reason": "no verified executed experiment"}
            path.write_text(json.dumps(report, indent=2) + "\n")
            return report
        slices = self._review_items(kb, 2)
        prepared = [self._reassess_item(kb, row, slices, executed) for row in executed]
        args = {
            "phase": "reassess",
            "run_id": self.run_id,
            "objective": self.config.objective,
            "schema": agent_schema(ReassessOutput),
            "model": self.config.phases.reassess.model,
            "web": {"searches": 0, "fetches": 0, "session_searches": 0},
            "executions": [{"execution": item["shown"], "hypothesis": item["context"]} for item in prepared],
        }
        outcome = self.launcher.run("reassess", step, args, self.run_dir)
        report: dict[str, Any] = {
            "status": "unverified",
            "judgements": [],
            "judged_count": 0,
            "launch": outcome.launch,
            "agent_outputs": outcome.result.get("outputs", []),
            "dropped_agents": outcome.result.get("dropped", []),
        }
        by_agent = {item.get("agent_id"): item.get("output") for item in report["agent_outputs"]}
        for item in prepared:
            row = item["row"]
            eid = row["experiment_id"]
            entry: dict[str, Any] = {
                "experiment_id": eid,
                "hypothesis_id": row["hypothesis_id"],
                "status": "unverified",
            }
            output = by_agent.get(f"reassessor@{eid}")
            if output is None:
                entry["error"] = f"no independent reassessor output for {eid}"
            else:
                try:
                    judgement = ReassessOutput.model_validate(output)
                    if reasons := _binding_failures(
                        judgement, row, row["result_digest"], item["result_claims"], item["allowed"]
                    ):
                        raise ValueError("reassessment does not bind to this result: " + "; ".join(reasons))
                    entry.update(status="judged", judgement=judgement.model_dump())
                except (ValidationError, ValueError, KeyError) as err:
                    entry["error"] = str(err)
            report["judgements"].append(entry)
        report["judged_count"] = sum(entry["status"] == "judged" for entry in report["judgements"])
        report["status"] = "judged" if report["judged_count"] else "unverified"
        path.write_text(json.dumps(report, indent=2) + "\n")
        return report

    def _reassess_item(
        self, kb: KnowledgeBase, row: dict[str, Any], slices: list[dict[str, Any]], executed: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """What one reassessor is shown, and the set its judgement is checked against — the same set."""
        hid = row["hypothesis_id"]
        context = next((copy.deepcopy(item) for item in slices if item["hypothesis_id"] == hid), None)
        if context is None:
            raise PhaseFailed(f"reassess: hypothesis {hid} has no context")
        # The claims of this execution are the ones the store holds for it, whether this run wrote them or
        # recognised them as facts it already had: the second case is a repeated deterministic analysis, and
        # the hypothesis waiting on it is new even when the result is not.
        # `result_claims` is recorded since reuse was added, because a reuse wrote nothing and its claims are the
        # computing EXP's; an archived report without it derives the same set.
        result_claims = row.get("result_claims") or [*row["written"], *sorted(row["seen_again"].values())]
        context["computed_result_claims"] = [
            kb.records[cid].model_dump(mode="json") for cid in result_claims if cid in kb.records
        ]
        # What the judgement may cite is named, not inferred: the agent is shown the hypothesis's whole
        # neighbourhood, so a critic that reads it cites from it — an earlier run's reassessor cited `C-1139`,
        # a verified literature claim of that slice, and a guard built on the tested claims alone refused it
        # . The set the guard checks is derived from this same field, so the two cannot disagree.
        context["citable_context_claims"] = [
            record["id"] for record in context["context"] if isinstance(kb.records.get(record["id"]), Claim)
        ]
        # `seen_again`'s keys are IDs the store dropped and never holds: the agent is shown only the IDs it kept,
        # so it cannot cite one that does not exist. The archived report keeps the map. The critic is shown its
        # own execution alone, not the phase report, so it cannot mistake another EXP's digest for this one's.
        shown = {
            key: row[key]
            for key in (
                "experiment_id",
                "hypothesis_id",
                "analysis_id",
                "tested_claims",
                "result_digest",
                "artifact",
                "interpretation_check",
                "written",
            )
        }
        shown |= {"status": "executed", "seen_again": sorted(row["seen_again"].values())}
        # One measurement can serve several hypotheses: each critic is told which, so three
        # judgements of one result are not read — by them or after them — as three replications.
        computation = row.get("result_of") or row["experiment_id"]
        shown["result_of"] = row.get("result_of")
        shown["shared_with"] = [
            {"experiment_id": other["experiment_id"], "hypothesis_id": other["hypothesis_id"]}
            for other in executed
            if other is not row and (other.get("result_of") or other["experiment_id"]) == computation
        ]
        return {
            "row": row,
            "shown": shown,
            "context": context,
            "result_claims": result_claims,
            "allowed": (set(row["tested_claims"]) | set(result_claims) | set(context["citable_context_claims"])),
        }

    def _batches(
        self, phase: str, output: Any, item: dict[str, Any], agent_id: str, kb: KnowledgeBase, report: dict[str, Any]
    ) -> list:
        def builder() -> Builder:
            return Builder(kb.records, kb.allocate, self.run_id, agent_id, self.clock(), self.verifier)

        if phase == "literature":
            return [builder().literature(output)]
        if phase == "generate":
            return [builder().hypothesis(output)]
        if phase in PER_HYPOTHESIS:
            hid = str(item.get("hypothesis_id"))
            if not isinstance(kb.records.get(hid), Hypothesis):
                raise ValueError(f"{phase} of unknown hypothesis {hid}")
            if phase == "review":
                report["summaries"][f"{hid}/{agent_id}"] = output.summary
                return [builder().review(output, hid)]
            if phase == "design":
                if not output.experiments:
                    report.setdefault("no_experiment", {})[hid] = output.no_experiment_reason or "no reason given"
                return [builder().experiments(output, hid, datasets=self._declared_datasets())]
            if phase == "intervene":
                if not output.candidates:
                    report.setdefault("no_candidate", {})[hid] = output.no_candidate_reason or "no reason given"
                report.setdefault("candidates", {})[hid] = len(output.candidates)
                return [builder().interventions(output, hid)]
            report.setdefault("decisions", {})[hid] = output.decision
            return [builder().decision(output, hid)]
        if phase == "synthesize":
            # One ID per citation tag is what the gate reads; a salvageable tag is rewritten and the rewrite is
            # reported, never hidden. What survives the rewrite fails the gate.
            if rewritten := normalize(output):
                report["normalized_tags"] = rewritten
            # The verify phase's verdicts carry `faithful`, which the knowledge base never stores: read them
            # back from the archived report so the gate can name a citation its own verifier questioned.
            verify_report = self._read_report("verify")
            feasibility = self._feasibility()["experiments"]
            execution = self._read_report("execute") or {"status": "not_executed"}
            reassessment = self._read_report("reassess") or {"status": "not_reassessed"}
            if execution.get("status") == "executed":
                from analysis.verify import verify_receipt

                # A reuse has no receipt of its own: it is bound to its runner's, which is checked once.
                for runner in dict.fromkeys(
                    row.get("result_of") or row["experiment_id"]
                    for row in execution["executions"]
                    if row.get("status") == "executed"
                ):
                    verify_receipt(self.run_dir / "experiment-analysis" / runner / "verification.json")
            result = gate(output, kb.records, self.run_id, (verify_report or {}).get("verdicts") or {}, execution)
            (self.run_dir / "final-proposal.json").write_text(
                json.dumps(
                    {
                        "run_id": self.run_id,
                        "agent_id": agent_id,
                        "proposal": output.model_dump(),
                        "gate": result,
                        "experiment_feasibility": feasibility,
                        "execution": execution,
                        "reassessment": reassessment,
                    },
                    indent=2,
                )
            )
            # The chains are read from the knowledge base by the host, not written by the PI: the section sits beside
            # the PI's prose and says what each step it cites is worth (`knowledge_base/chains.py`).
            chains = causal_chains(kb.records, self.run_id, output.model_dump())
            (self.run_dir / "final-proposal.md").write_text(
                render(output, result, self.run_id, feasibility, execution, reassessment, chains)
            )
            report["proposal"] = {"outcome": output.outcome, "gate": result}
            return []
        # verify: one verified copy per confirmed relation that is still a candidate
        candidates = {c["relation_id"] for c in self._verify_candidates(kb)}
        batches = []
        for verdict in output.verdicts:
            report["verdicts"][verdict.relation_id] = verdict.model_dump() | {"agent_id": agent_id}
            if verdict.relation_id not in candidates or verdict.verdict != "confirmed":
                continue
            if verdict.faithful:
                batches.append(builder().confirmed(kb.records[verdict.relation_id], verdict))
                continue
            # Confirmed on the quote, but the claim says more than the quote: the verifier's restatement becomes
            # a narrower verified claim rather than nothing at all (`Builder.narrowed`). Why it could not be
            # done is reported, never swallowed.
            try:
                batches.append(builder().narrowed(kb.records[verdict.relation_id], verdict))
            except ValueError as err:
                report["not_narrowed"][verdict.relation_id] = str(err)
        return batches
