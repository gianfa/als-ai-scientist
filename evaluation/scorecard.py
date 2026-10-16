"""One row per evaluation dimension, and the words `no scorer` where there is none.

Why this exists. Both the scientific result and the AI system are evaluated, over eighteen dimensions. This
module scores what the archived artifacts support and says plainly, in those words, where nothing can be scored:
a plausible number over the wrong denominator is worse than an admission.

    uv run python -m evaluation.scorecard experiments/<RUN>/results [--kb ../knowledgebase]
        [--audit <audit/citations.md>] [--format markdown]

It computes almost nothing itself: every number comes from a module that already owns its definition
(`run_score`, `tool_usage`, `refine_entry`, `citation_audit`, `knowledge_base.status`), so the repository has one
definition of each measurement and this file only arranges them. Three kinds of row, never mixed:

- **scored** — a value, the artifact it was read from, and the one limit a reader must hold;
- **`no scorer`** — the dimension and *why* nothing honest can be computed from a run's archive;
- **null with a reason** — a scorer exists but its input is missing from this archive (an unread audit, a
  phase that never ran). Never zero: zero is a measurement, absence is not.

What it refuses to do. It produces no overall score and no weighted average: the weights would be ours and
nobody could defend them. A row that needs a human (citation accuracy) stays empty until the human has read the
packet `citation_audit.py` prints.
"""

from __future__ import annotations

import argparse
import itertools
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from knowledge_base.schemas import Claim, Decision, Experiment, Hypothesis
from knowledge_base.status import superseded
from knowledge_base.store import read_records

from . import citation_audit, run_score, tool_usage
from .refine_entry import claims_of

DEFAULT_KB: Final = Path(__file__).resolve().parents[1] / "knowledgebase"

# The dimensions with no honest scorer over a run's archive, each with the reason it has none. Kept as data so a
# reader can see the list and so a test can assert it is still there: the temptation is to quietly fill one in.
NO_SCORER: Final[dict[str, tuple[str, str]]] = {
    "EVAL-S2": (
        "Statistical robustness",
        "only the deterministic analyses carry statistics, and an LLM's prose has nothing to be robust "
        "about; the analyses' own tests cover their numbers",
    ),
    "EVAL-S3": ("Biological plausibility", "requires a domain expert; no run has had one"),
    "EVAL-S5": ("External validation", "requires a dataset outside this pipeline, which none of it has touched"),
}


def _row(rid: str, name: str, value: Any, source: str, limit: str) -> dict[str, Any]:
    return {"id": rid, "dimension": name, "value": value, "from": source, "limit": limit, "status": "scored"}


def _unmeasured(rid: str, name: str, reason: str, source: str = "") -> dict[str, Any]:
    return {"id": rid, "dimension": name, "value": None, "from": source, "limit": reason, "status": "null"}


def reproducibility(score: Mapping[str, Any], results_dir: Path) -> dict[str, Any]:
    state = run_score.read_json(results_dir / "state.json") or {}
    phases = state.get("phases") or {}
    archived = sum(1 for p in phases.values() if isinstance(p, dict) and (p.get("launch") or {}).get("stage"))
    compute = run_score.read_json(results_dir / "02-compute-report.json") or {}
    analyses = compute.get("analyses") or {}
    digested = sum(1 for a in analyses.values() if isinstance(a, dict) and a.get("result_digest"))
    return _row(
        "EVAL-S1",
        "Reproducibility",
        {
            "config_hash": state.get("config_hash"),
            "phases_with_an_archived_launch": archived,
            "analyses_run": len(analyses),
            "analyses_with_a_result_digest": digested,
        },
        "state.json, 02-compute-report.json",
        "the deterministic layer is reproducible by digest; the LLM phases are not bit-reproducible, "
        "and nothing here claims they are",
    )


def literature_support(audit: Mapping[str, Any] | None) -> dict[str, Any]:
    if not audit:
        return _unmeasured(
            "EVAL-S4",
            "Independent literature support",
            "needs the audit packet: run `evaluation.citation_audit --save` first",
            "audit/citations.json",
        )
    by_rung = audit["ladder"]["by_rung"]
    claim_tags = audit["ladder"]["total"] - by_rung["derived"]
    return _row(
        "EVAL-S4",
        "Independent literature support",
        {
            "claim_citation_tags": claim_tags,
            "with_a_checked_quote": by_rung["quote_checked"],
            "quote_whose_source_text_is_absent": by_rung["source_text_absent"],
            "quote_with_no_source_ref": by_rung["no_source_ref"],
            "no_quote": by_rung["no_quote"],
        },
        "audit/citations.json (the support ladder)",
        "a checked quote means the passage is in the stored paper, nothing about whether it supports "
        "the claim; that is the human pass of EVAL-AI2",
    )


def falsifiability(
    records: Mapping[str, Any], run_id: str | None, phase_reports: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any]:
    """Not "how many stored experiments are falsifying" — `Experiment._rules` refuses one that is not, so that
    ratio is 1.0 by construction and reporting it would be an invented favourable metric. What can be measured is
    the **gate's** work: how many proposals it refused for not naming an outcome that would contradict a tested claim, against how many were written.
    """
    mine = [r for r in records.values() if isinstance(r, Experiment) and r.run_id == run_id]
    design = phase_reports.get("design") or {}
    messages = [m for msgs in (design.get("dropped_elements") or {}).values() for m in msgs]
    refused = [m for m in messages if "not falsifying" in m]
    if not mine and not messages:
        return _unmeasured(
            "EVAL-S6", "Falsifiability", "this run proposed no experiment", "knowledge base, the design report"
        )
    return _row(
        "EVAL-S6",
        "Falsifiability",
        {
            "experiments_written": len(mine),
            "proposals_refused_as_not_falsifying": len(refused),
            "proposals_refused_for_other_reasons": len(messages) - len(refused),
        },
        "knowledge base (EXP- records), the design phase report",
        "every stored experiment names a contradicting outcome **by construction**: the schema refuses "
        "one that does not, so this row counts the refusals, not a property of the survivors. Whether "
        "an experiment could really produce that outcome is not judged anywhere",
    )


def tractability(records: Mapping[str, Any], run_id: str | None) -> dict[str, Any]:
    mine = [r for r in records.values() if isinstance(r, Experiment) and r.run_id == run_id]
    if not mine:
        return _unmeasured("EVAL-S8", "Experimental tractability", "this run proposed no experiment", "knowledge base")

    def tally(attr: str) -> dict[str, int]:
        out: dict[str, int] = {}
        for e in mine:
            out[getattr(e, attr)] = out.get(getattr(e, attr), 0) + 1
        return out

    return _row(
        "EVAL-S8",
        "Experimental tractability",
        {"cost": tally("cost"), "feasibility": tally("feasibility"), "kind": tally("kind")},
        "knowledge base (EXP- records)",
        "the agent's own qualitative judgement, by design: a figure in USD from an LLM is not checkable",
    )


def hallucination_surface(score: Mapping[str, Any], audit: Mapping[str, Any] | None) -> dict[str, Any]:
    gate = score.get("result") or {}
    unknown = gate.get("gate_unknown_ids")
    quotes = None
    if audit:
        rungs = audit["ladder"]["by_rung"]
        quotes = {
            "checked": rungs["quote_checked"],
            "source_text_absent": rungs["source_text_absent"],
            "no_source_ref": rungs["no_source_ref"],
        }
    losses = score.get("losses") or {}
    return _row(
        "EVAL-AI1",
        "Hallucination surface",
        {
            "cited_ids_the_gate_could_not_resolve": unknown,
            "quotes_of_cited_claims": quotes,
            "records_refused_at_write_time": losses.get("rejected"),
            "elements_refused_by_validation": losses.get("dropped_elements"),
        },
        "final-proposal.json (gate), audit/citations.json, the phase reports",
        "a surface, not a rate: it counts what the machinery could check. A claim that is faithful to a "
        "quote and wrong about the world is invisible to every number here",
    )


def citation_accuracy(verdicts: Mapping[str, Any] | None) -> dict[str, Any]:
    if not verdicts:
        return _unmeasured(
            "EVAL-AI2",
            "Citation accuracy",
            "the audit packet has not been read; this row stays empty until a human fills the "
            "verdict column (`evaluation.citation_audit --verdicts`)",
            "audit/citations.md",
        )
    return _row(
        "EVAL-AI2",
        "Citation accuracy",
        {
            "read": verdicts["read"],
            "counts": verdicts["counts"],
            "accuracy": verdicts["accuracy"],
            "denominator": verdicts["denominator"],
            "undecidable_by_reading": verdicts["undecidable_by_reading"],
        },
        "audit/citations.md, read by hand",
        "a **textual** judgement, whether the quote says what the claim says — not whether the biology "
        "is right, which is EVAL-S3 and has no scorer. One proposal, one reader, and the reader is the "
        "system's author; rows cited to limit the argument, and rows a reader could not decide without "
        "knowing the field, leave the denominator and are reported apart",
    )


def provenance(score: Mapping[str, Any], results_dir: Path) -> dict[str, Any]:
    phases = score.get("phases") or {}
    args = sorted((results_dir / "workflows").glob("*.args.json")) if (results_dir / "workflows").is_dir() else []
    return _row(
        "EVAL-AI5",
        "Reproducibility of the system",
        {
            "phases_completed": len(phases.get("completed") or []),
            "phases_missing": phases.get("missing"),
            "archived_phase_arguments": len(args),
        },
        "state.json, workflows/*.args.json",
        "every phase's prompt arguments and schema are archived, so a phase can be replayed; the model's "
        "answer cannot be reproduced",
    )


def tool_use(results_dir: Path) -> dict[str, Any]:
    calls = tool_usage.read_calls(results_dir)
    if not calls:
        return _unmeasured(
            "EVAL-AI6", "Tool-use accuracy", "no agent tool calls archived in this run", "*-agent-tools.jsonl"
        )
    summary = tool_usage.summarize(calls)
    diagnosis = tool_usage.diagnose(calls)
    return _row(
        "EVAL-AI6",
        "Tool-use accuracy",
        {
            "calls": summary.get("calls"),
            "by_tool": summary.get("tools"),
            "failed": len(diagnosis.get("failed_calls") or []),
            "empty": len(diagnosis.get("empty_results") or []),
            "repeated": len(diagnosis.get("repeated_calls") or []),
            "rows_without_an_outcome": len(diagnosis.get("rows_without_outcome") or []),
        },
        "*-agent-tools.jsonl",
        "counts, not a ratio: 'a usable result' has no definition we could defend, so the ratio was "
        "dropped on review. An empty answer may be a correct question about an empty knowledge base",
    )


def self_detection(
    score: Mapping[str, Any], audit: Mapping[str, Any] | None, verdicts: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    losses = score.get("losses") or {}
    stated = None
    if audit:
        stated = {"known_weaknesses": len(audit.get("weaknesses") or []), "stated_by_the_proposal": None}
        if verdicts and verdicts.get("weaknesses_read"):
            stated |= {
                "read": verdicts["weaknesses_read"],
                "stated_by_the_proposal": verdicts["weaknesses_stated"],
                "not_stated": verdicts["weaknesses_not_stated"],
            }
    return _row(
        "EVAL-AI7",
        "Detection of its own errors",
        {
            "elements_our_validation_refused": losses.get("dropped_elements"),
            "whole_agent_outputs_refused": losses.get("invalid_outputs"),
            "elements_converted_instead_of_dropped": losses.get("converted_elements"),
            "weaknesses": stated,
        },
        "the phase reports, audit/citations.json",
        "the first two numbers are **us** catching the agents, not the system catching itself. The third is the "
        "only honest measure: of the weaknesses the artifacts know about, how many the proposal states",
    )


def rejection(records: Mapping[str, Any], run_id: str | None) -> dict[str, Any]:
    mine = [r for r in records.values() if isinstance(r, Decision) and r.run_id == run_id]
    if not mine:
        return _unmeasured(
            "EVAL-AI8", "Rejection of unsupported hypotheses", "this run recorded no decision", "knowledge base"
        )
    by_kind: dict[str, int] = {}
    for d in mine:
        by_kind[d.decision] = by_kind.get(d.decision, 0) + 1
    return _row(
        "EVAL-AI8",
        "Rejection of unsupported hypotheses",
        {"decisions": by_kind, "value_of_information": sorted({d.value_of_information for d in mine})},
        "knowledge base (D- records)",
        "declining is the cheaper outcome, so a high rejection count is not by itself discrimination; "
        "the rationales are in the records and nobody has read them",
    )


def hypothesis_diversity(records: Mapping[str, Any], run_id: str | None) -> dict[str, Any]:
    """Not the dropped metric (claim-set overlap, a scorer that was rejected): that one
    measured how much input context two hypotheses shared, not their independence. This measures a different,
    output-side question: whether two hypotheses assert structurally distinct mechanisms, by comparing the
    causal edges (`cause_ref`, `effect_ref`) of the claims each has `part_of` it, not the words either uses.

    A pair with no causal claim on either side counts as fully similar (similarity 1.0, zero contribution to
    diversity), the convention the previous architecture's diversity metric used for a textually empty pair.
    """
    mine = [r for r in records.values() if isinstance(r, Hypothesis) and r.run_id == run_id]
    old = superseded(records)
    live = [h for h in mine if h.id not in old]
    if len(live) < 2:
        return _unmeasured(
            "EVAL-AI4",
            "Hypothesis diversity",
            "fewer than two live hypotheses of this run in the knowledge base",
            "knowledge base",
        )
    parts = claims_of(records)
    edge_sets = []
    for h in live:
        claims = (records.get(cid) for cid in parts.get(h.id, []))
        edge_sets.append(
            {(c.cause_ref, c.effect_ref) for c in claims if isinstance(c, Claim) and c.causal_fields() is not None}
        )
    pairs = list(itertools.combinations(edge_sets, 2))
    similarities = [len(a & b) / len(a | b) if a | b else 1.0 for a, b in pairs]
    diversity = round(1 - sum(similarities) / len(similarities), 4)
    return _row(
        "EVAL-AI4",
        "Hypothesis diversity",
        {
            "hypotheses": len(live),
            "pairs_compared": len(pairs),
            "with_a_causal_claim_of_their_own": sum(1 for s in edge_sets if s),
            "diversity": diversity,
        },
        "knowledge base (H- hypotheses, their own causal C- claims via `part_of`, excluding superseded)",
        "measures distinct asserted mechanisms, not textual novelty and not whether the evidence pool "
        "was shared (the overlap-of-claim-sets variant this row used to carry no scorer for); a "
        "hypothesis with no causal claim of its own counts as fully similar to every other such "
        "hypothesis by convention, not by having been compared",
    )


def hypothesis_novelty(results_dir: Path) -> list[dict[str, Any]]:
    """EVAL-S7 and EVAL-AI3 from `13-novelty-report.json`, written by `python -m pipeline.cli novelty`.

    This does not measure novelty: it counts the hypotheses for which an agent, searching OpenAlex, found a work
    that states them (`anticipated`), parts of them (`partly_anticipated`) or none it read (`no_prior_art_found`).
    The last is the closest this can come to "new", and it is a statement about these queries on this index on this
    date. The same measurement serves both rows because the statement lists novelty twice, once for the result and
    once for the system. No report on this archive: unmeasured, never zero.
    """
    report = run_score.read_json(results_dir / "13-novelty-report.json")
    rows = []
    for rid, name in (("EVAL-S7", "Novelty"), ("EVAL-AI3", "Hypothesis novelty")):
        if not isinstance(report, dict) or not isinstance(report.get("entries"), list):
            rows.append(
                _unmeasured(
                    rid,
                    name,
                    "the novelty stage has not been run on this archive " "(`python -m pipeline.cli novelty`)",
                    "13-novelty-report.json",
                )
            )
            continue
        entries = report["entries"]
        verdicts = [(e.get("novelty") or {}).get("verdict") for e in entries if e.get("status") == "assessed"]
        # The host's per-work decisions. Absent from a report written before it recorded them: unknown, not zero.
        blocks = [e["provenance"] for e in entries if isinstance(e.get("provenance"), dict)]
        assessed_blocks = [
            e["provenance"] for e in entries if e.get("status") == "assessed" and isinstance(e.get("provenance"), dict)
        ]
        # One work two hypotheses both cite is one paper read, so the per-entry counts cannot be summed into a
        # count of papers: the sum answers "how deep does each verdict go", this union "how many works the
        # stage read at all". It is comparable with the diagnosis's `papers_opened`; the sum is not.
        read_in_full = {
            w.get("openalex_id")
            for b in assessed_blocks
            for w in (b.get("kept") or [])
            if w.get("evidence_depth") == "full_text"
        }
        value = {
            "hypotheses": len(entries),
            "assessed": len(verdicts),
            "unassessed": len(entries) - len(verdicts),
            "full_texts_read": sum(
                len((e.get("novelty") or {}).get("full_texts_read") or [])
                for e in entries
                if e.get("status") == "assessed"
            ),
            "works_listed": sum(b.get("works_listed") or 0 for b in assessed_blocks) if blocks else None,
            "works_read_in_full": (sum(b.get("works_read_in_full") or 0 for b in assessed_blocks) if blocks else None),
            "distinct_works_read_in_full": len(read_in_full) if blocks else None,
            # A drop is a fact about provenance, so it counts whether or not the entry kept a verdict.
            "works_dropped": sum(b.get("works_dropped") or 0 for b in blocks) if blocks else None,
            **{v: verdicts.count(v) for v in ("anticipated", "partly_anticipated", "no_prior_art_found")},
            "sources_searched": report.get("sources_searched"),
            "searched_at": report.get("searched_at"),
        }
        rows.append(
            _row(
                rid,
                name,
                value,
                "13-novelty-report.json (agent reading of OpenAlex abstracts)",
                "not a measure of novelty: `no_prior_art_found` means the queries run on the sources "
                "named returned no work stating the hypothesis, and says nothing about full texts, other "
                "indexes or later work; unless `full_texts_read` says otherwise the verdict rests on "
                "abstracts alone; the verdict is an agent's reading and its error rate is not "
                "measured; every work counted in `works_listed` was served by the search tool, while "
                "`works_dropped` counts the works the host dropped because it was not, so an "
                "`assessed` entry does not mean every work the agent named survived; "
                "`works_read_in_full` counts, per hypothesis, the kept works whose paper that "
                "hypothesis's own agent declared in `full_texts_read` and this stage's `read_paper` "
                "log confirms, so it says how deep a single verdict goes and not how many papers the "
                "stage opened — two hypotheses citing the same full text make the sum count it twice, "
                "which is what `distinct_works_read_in_full` counts instead; a work another agent "
                "opened, or one read and not declared, reads as `abstract` here, so the figure is a "
                "floor; `full_texts_read` likewise sums each agent's own declared list, so a paper "
                "two agents both opened counts once for each; `unassessed` entries had no valid "
                "agent output",
            )
        )
    return rows


def cost_and_latency(score: Mapping[str, Any], results_dir: Path) -> list[dict[str, Any]]:
    cost = score.get("cost") or {}
    stages = cost.get("stages") or {}
    state = run_score.read_json(results_dir / "state.json") or {}
    wall = {
        name: (phase.get("launch") or {}).get("wall_s")
        for name, phase in (state.get("phases") or {}).items()
        if isinstance(phase, dict) and (phase.get("launch") or {}).get("wall_s") is not None
    }
    return [
        _row(
            "EVAL-AI9",
            "Computational cost",
            {
                "total_usd": cost.get("total_usd"),
                "usd_by_stage": {k: v.get("usd") for k, v in stages.items()},
                "agents_by_stage": {k: v.get("agents") for k, v in stages.items()},
            },
            "the per-phase result files",
            "the CLI's own cost figures; the cost watcher has under-read by 16-22% in the past",
        ),
        _row(
            "EVAL-AI10",
            "Latency",
            {"wall_s_by_phase": wall, "total_wall_s": round(sum(wall.values()), 1) if wall else None},
            "state.json (each phase's launch record)",
            "agent wall time per phase; the launcher's own overhead is inside it and is not separated",
        ),
    ]


def scorecard(results_dir: Path, kb_root: Path, audit_file: Path | None = None) -> dict[str, Any]:
    score = run_score.score(results_dir, kb_root)
    records = read_records(kb_root)
    run_id = score.get("run_id")
    audit_json = results_dir / "audit" / "citations.json"
    audit = json.loads(audit_json.read_text()) if audit_json.is_file() else None
    verdicts = citation_audit.read_verdicts(audit_file) if audit_file and audit_file.is_file() else None
    if verdicts is not None and not verdicts.get("read"):
        verdicts = None
    phase_reports = run_score.reports(results_dir)
    rows = [
        reproducibility(score, results_dir),
        literature_support(audit),
        falsifiability(records, run_id, phase_reports),
        tractability(records, run_id),
        hallucination_surface(score, audit),
        citation_accuracy(verdicts),
        provenance(score, results_dir),
        tool_use(results_dir),
        self_detection(score, audit, verdicts),
        rejection(records, run_id),
        hypothesis_diversity(records, run_id),
        *hypothesis_novelty(results_dir),
        *cost_and_latency(score, results_dir),
    ]
    rows += [
        {"id": rid, "dimension": name, "value": None, "from": "", "limit": reason, "status": "no scorer"}
        for rid, (name, reason) in NO_SCORER.items()
    ]
    rows.sort(key=lambda r: (r["id"].split("-")[1][:2], int("".join(c for c in r["id"] if c.isdigit()))))
    return {
        "run_id": run_id,
        "results_dir": str(results_dir),
        "kb": str(kb_root),
        "audit_read": bool(verdicts),
        "rows": rows,
        "counts": {k: sum(1 for r in rows if r["status"] == k) for k in ("scored", "null", "no scorer")},
    }


def render(data: Mapping[str, Any]) -> str:
    lines = [
        f"# Evaluation scorecard — {data.get('run_id')}",
        "",
        "One row per evaluation dimension. **`no scorer`** means no honest number can be "
        "computed from a run's archive, and the reason is given; **`—`** means a scorer exists but its "
        "input is missing from this archive. There is no overall score: the weights would be ours.",
        "",
        "| id | dimension | value | status | read from | limit |",
        "|---|---|---|---|---|---|",
    ]
    for row in data["rows"]:
        value = (
            "`no scorer`"
            if row["status"] == "no scorer"
            else ("—" if row["value"] is None else f"`{json.dumps(row['value'], default=str)}`")
        )
        lines.append(
            f"| {row['id']} | {row['dimension']} | {value} | {row['status']} | {row['from'] or '-'} | "
            f"{row['limit']} |"
        )
    counts = data["counts"]
    lines += [
        "",
        f"{counts['scored']} scored, {counts['null']} unmeasured on this archive, "
        f"{counts['no scorer']} with no scorer at all.",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("results_dir", type=Path)
    parser.add_argument("--kb", type=Path, default=DEFAULT_KB)
    parser.add_argument("--audit", type=Path, help="a filled audit packet, for EVAL-AI2")
    parser.add_argument("--format", choices=("json", "markdown"), default="markdown")
    args = parser.parse_args(argv)
    data = scorecard(args.results_dir, args.kb, args.audit)
    print(render(data) if args.format == "markdown" else json.dumps(data, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
