"""Score one run of the phase pipeline from its archive: the measurements a pre-registration is judged against.

Why this exists. The per-run numbers that decide whether a change worked — did the hypothesis authors cite the
evidence this run's own literature phase produced, did a refused objection survive, did an agent cite an ID a tool
returned rather than one the orchestrator pushed, did the blinding hold — must be reproducible, so evaluation
operates on persisted artifacts only. Every number below is read from the run directory (and, where relations are
needed, from the knowledge base); none is invented, and a measurement whose inputs are missing is reported as
`null` with the reason rather than as zero.

    uv run python -m evaluation.run_score <run-results-dir> [--kb ../knowledgebase] [--format markdown]

What it does *not* do: judge. There is no score, no threshold and no verdict here — the predictions live in the
pre-registration, and a human compares them with this output. Two limits worth knowing when reading it:
`result_preview` in the tool log is truncated (300 bytes), so anything read from a tool's answer is a lower
bound; and the knowledge base is shared across runs, so a claim written by an earlier run is not "fresh" here
even when it is new to the reader.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
# `PACKAGE_ROOT` is the repository root.
DEFAULT_KB = PACKAGE_ROOT / "knowledgebase"
# Who wrote a record and how sure they were. `review` is blinded, so neither may reach an agent through a pushed
# slice or a tool answer. An author stating its *own* confidence is not a leak, so the output schema of a phase
# (`args.schema`) and what an agent sent to `StructuredOutput` (`input_preview`) are excluded from the check.
AUTHOR_FIELDS = ("agent_id", "author_confidence")
# Tools that touch the filesystem. Only `Read` is granted today (the Workflow tool refuses a scriptPath it
# cannot read); the others are listed so that a run from before a restriction, or a future grant, is measured
# rather than missed. `Grep` and `Glob` take no path argument and so are counted but never located.
FILESYSTEM_TOOLS = ("Read", "Grep", "Glob", "Bash", "Edit", "Write", "NotebookEdit")
# The CLI's own transcript tree. A session may read it whatever its working directory, so a call landing here
# is neither inside the phase directory nor a reach the restriction can deny: `filesystem_reach` counts it apart.
CLI_TRANSCRIPTS = "/.claude/projects/"
PHASE_ORDER = (
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


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.is_file():
        return rows
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def reports(results_dir: Path) -> dict[str, dict[str, Any]]:
    """The phase reports of a run, keyed by phase name. A re-attempt writes over its phase's report, which is
    what the orchestrator did, so the key is the phase and the archive keeps the attempt files beside it."""
    found: dict[str, dict[str, Any]] = {}
    for path in sorted(results_dir.glob("*-report.json")):
        phase = path.name.split("-", 1)[1][: -len("-report.json")]
        report = read_json(path)
        if report is not None:
            found[phase] = report
    return found


def written_ids(report: dict[str, Any], prefix: str) -> set[str]:
    return {r for r in (report.get("written") or []) if isinstance(r, str) and r.startswith(prefix)}


def cost(results_dir: Path) -> dict[str, Any]:
    """What the run cost, from the CLI's own per-session figure in each `<stage>-result.json`."""
    stages: dict[str, dict[str, Any]] = {}
    total = 0.0
    for path in sorted(results_dir.glob("*-result.json")):
        record = read_json(path)
        if record is None:
            continue
        usd = record.get("cost_usd")
        usd = float(usd) if isinstance(usd, (int, float)) else None
        stages[path.name[: -len("-result.json")]] = {
            "usd": usd,
            "agents": record.get("agent_count"),
            "status": record.get("status"),
        }
        total += usd or 0.0
    return {"total_usd": round(total, 3), "stages": stages}


def losses(phase_reports: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Elements, not agents. `state.json` counts the agents affected, which can read three where twenty-five
    elements were lost, so this counts the messages themselves and keeps them for reading."""

    def elements(key: str) -> dict[str, int]:
        out: dict[str, int] = {}
        for phase, report in phase_reports.items():
            n = sum(len(v) for v in (report.get(key) or {}).values())
            if n:
                out[phase] = n
        return out

    messages = {
        phase: sorted(m for agent in (report.get("dropped_elements") or {}).values() for m in agent)
        for phase, report in phase_reports.items()
        if report.get("dropped_elements")
    }
    kept = {
        phase: sorted(m for agent in (report.get("converted_elements") or {}).values() for m in agent)
        for phase, report in phase_reports.items()
        if report.get("converted_elements")
    }
    return {
        "dropped_elements": elements("dropped_elements"),
        "dropped_messages": messages,
        "converted_elements": elements("converted_elements"),
        "converted_messages": kept,
        "invalid_outputs": {
            p: sorted((r.get("invalid_outputs") or {})) for p, r in phase_reports.items() if r.get("invalid_outputs")
        },
        "dropped_agents": {p: r["dropped_agents"] for p, r in phase_reports.items() if r.get("dropped_agents")},
        "rejected": {
            p: sum(len(v) for v in (r.get("rejected") or {}).values())
            for p, r in phase_reports.items()
            if r.get("rejected")
        },
    }


def evidence_reach(phase_reports: dict[str, dict[str, Any]], relations: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Of the claims this run's `literature` phase wrote, how many any later phase actually linked to.

    If the hypothesis authors are shown only older verified claims, this is 0 and the run paid for a literature
    phase that fed nothing. Needs the relations of the
    knowledge base, because a phase report lists the relation IDs it wrote, not their endpoints.
    """
    literature = phase_reports.get("literature")
    if literature is None:
        return {"fresh_claims": None, "reason": "no literature report in this archive"}
    fresh = written_ids(literature, "C-")
    if not relations:
        return {"fresh_claims": len(fresh), "touched": None, "reason": "relations not available"}
    by_phase: dict[str, list[str]] = {}
    for phase, report in phase_reports.items():
        if phase == "literature":
            continue
        touched = set()
        for rid in written_ids(report, "L-"):
            relation = relations.get(rid)
            if relation is None:
                continue
            for end in (relation.get("source_id"), relation.get("target_id")):
                if end in fresh:
                    touched.add(end)
        if touched:
            by_phase[phase] = sorted(touched)
    ever = sorted({c for ids in by_phase.values() for c in ids})
    return {
        "fresh_claims": len(fresh),
        "touched_by_any_later_phase": len(ever),
        "touched_by_authors": len(by_phase.get("generate", [])),
        "by_phase": by_phase,
        "claims": ever,
    }


def slice_and_citations(
    results_dir: Path, phase_reports: dict[str, dict[str, Any]], relations: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """What the hypothesis authors were shown, and where the claims they cited came from.

    A citation that is in neither the pushed slice nor the prior graph came from a tool call: that is the only
    direct evidence that an agent believes it may cite what a tool returns.
    """
    args_files = sorted(results_dir.glob("workflows/*-generate.args.json"))
    if not args_files:
        return {"slice": None, "reason": "no generate args in this archive"}
    args = read_json(args_files[0]) or {}
    context = [c for c in (args.get("kb_context") or []) if isinstance(c, dict)]
    literature = phase_reports.get("literature") or {}
    fresh = written_ids(literature, "C-")
    slice_ids = {str(c.get("id")) for c in context}
    composition = {
        "claims": len(context),
        "verified": sum(1 for c in context if c.get("provenance_status") == "verified"),
        "computed": sum(1 for c in context if c.get("origin") == "computation"),
        "fresh": len(slice_ids & fresh),
    }
    generate = phase_reports.get("generate")
    if generate is None or not relations:
        return {
            "slice": composition,
            "citations": None,
            "reason": "no generate report" if generate is None else "relations not available",
        }
    prior = json.dumps(args.get("prior_graph") or {})
    own = written_ids(generate, "C-")
    cited: set[str] = set()
    for rid in written_ids(generate, "L-"):
        relation = relations.get(rid)
        if relation is None:
            continue
        for end in (relation.get("source_id"), relation.get("target_id")):
            if isinstance(end, str) and end.startswith("C-") and end not in own:
                cited.add(end)
    outside = sorted(c for c in cited if c not in slice_ids and c not in prior)
    return {
        "slice": composition,
        "citations": {
            "claims_cited": len(cited),
            "from_slice": len(cited & slice_ids),
            "from_neither_slice_nor_prior_graph": len(outside),
            "outside": outside,
        },
    }


def search_use(calls: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """How the agents used `kb_search`: how often, how often with `types`, how often answered with nothing.

    `types` was added so a search answers with the evidence rather than with the system's own records; whether
    the agents pass it is the only way to know the prompt line reached them.
    """
    from .tool_usage import _is_empty  # one definition of "answered nothing"

    per_phase: dict[str, Counter[str]] = {}
    totals: Counter[str] = Counter()
    for call in calls:
        if call.get("tool") != "mcp__kb__kb_search":
            continue
        stage = str(call.get("stage") or "?")
        counter = per_phase.setdefault(stage, Counter())
        for key in ("calls",):
            counter[key] += 1
            totals[key] += 1
        if '"types"' in str(call.get("input_preview") or ""):
            counter["with_types"] += 1
            totals["with_types"] += 1
        if call.get("status") == "ok" and _is_empty(
            str(call.get("result_preview") or ""), int(call.get("result_bytes") or 0)
        ):
            counter["empty"] += 1
            totals["empty"] += 1
    return {
        **{k: totals.get(k, 0) for k in ("calls", "with_types", "empty")},
        "by_stage": {stage: dict(counter) for stage, counter in sorted(per_phase.items())},
    }


def blinding(results_dir: Path, calls: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Every place an author field reached an agent, as far as the archive can show. Empty is expected.

    Checked: the records pushed into a prompt (`kb_context`, `items` of the phase args) and what a tool answered
    (`result_preview`). Not checked, because an author stating its own confidence is not a leak: the phase's
    output `schema`, and what an agent itself sent (`input_preview`).

    **The tool-answer leg is a lower bound, and knowing why matters.** `result_preview` is truncated to a few
    hundred bytes, so a field that appears late in a record is invisible here: a run where `kb_get` returned
    `agent_id` and `author_confidence` to reviewers can still score 0 leaks on this check. The guard against that defect is the unit test over the served
    tools (`tests/test_knowledge_base_mcp_server.py`), not this measurement; what this measurement catches is a
    leak in the pushed slice, which is complete, and a leak big enough to surface in a preview.
    """
    found: list[dict[str, Any]] = []
    for path in sorted(results_dir.glob("workflows/*.args.json")):
        args = read_json(path) or {}
        for key in ("kb_context", "items"):
            payload = json.dumps(args.get(key) or [])
            for field in AUTHOR_FIELDS:
                if f'"{field}"' in payload:
                    found.append({"where": f"{path.name}:{key}", "field": field})
    for call in calls:
        preview = str(call.get("result_preview") or "")
        for field in AUTHOR_FIELDS:
            if f'"{field}"' in preview:
                found.append({"where": f"{call.get('stage')}:{call.get('label')}:{call.get('tool')}", "field": field})
    return {"leaks": len(found), "where": found}


def filesystem_reach(results_dir: Path, calls: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Every filesystem call the agents made, and whether it stayed inside the phase's working directory.

    Blinding and the integrity chain both assume the agents reach data only through the served tools. A phase
    runs in `workflows/<stage>/`, which holds only its own script, and `--permission-mode dontAsk` denies a read
    outside the working directory; without that, `Read` would reach `dist/`, `data/` and
    `experiments/<RUN_ID>/results/*.jsonl`, where `agent_id` appears.

    So `outside_succeeded` is the number that matters, and **0 is the expected value**. Attempts are counted separately on purpose: a denial is the restriction doing its job, not a
    leak, but a run full of denied attempts says the prompts point agents at files they cannot have.

    `transcript` is counted apart because it is **not** closed by the working directory. The CLI lets a session
    read its own transcript tree under `~/.claude/projects/` whatever the cwd, and agents do use it, e.g. to read
    back a truncated `WebFetch` result in full. What the
    stage directory buys here is partition, not denial — the project directory is keyed by the cwd, so one phase
    cannot reach another phase's transcripts, and the authorship a `review` agent must not see stays in
    `generate`'s. Sibling agents of the *same* phase still share one tree; that bounds review independence, not
    author blinding.

    A path under `workflows/` is inside, and so is a relative one, since the working directory is the stage
    directory. `input_preview` is truncated, so a call whose path did not survive truncation is counted
    `unparsed` rather than guessed at: like the tool-answer leg of `blinding`, this is a lower bound.
    """
    inside_root = str((results_dir / "workflows").resolve())
    counts = Counter()
    outside: list[dict[str, Any]] = []
    for call in calls:
        tool = str(call.get("tool") or "")
        if tool not in FILESYSTEM_TOOLS:
            continue
        counts["calls"] += 1
        match = re.search(
            r'"(?:file_path|path|notebook_path)"\s*:\s*"((?:[^"\\]|\\.)*)"', str(call.get("input_preview") or "")
        )
        if match is None:
            counts["unparsed"] += 1
            continue
        path = match.group(1).replace("\\/", "/")
        if not path.startswith("/") or path.startswith(inside_root):
            counts["inside"] += 1
            continue
        if CLI_TRANSCRIPTS in path:
            counts["transcript"] += 1
            continue
        succeeded = call.get("status") == "ok"
        counts["outside_succeeded" if succeeded else "outside_refused"] += 1
        outside.append(
            {
                "where": f"{call.get('stage') or call.get('phase')}:{call.get('label')}:{tool}",
                "path": path,
                "succeeded": succeeded,
            }
        )
    return {
        "calls": counts["calls"],
        "inside": counts["inside"],
        "unparsed": counts["unparsed"],
        "transcript": counts["transcript"],
        "outside_succeeded": counts["outside_succeeded"],
        "outside_refused": counts["outside_refused"],
        "where": outside,
    }


def outcome(results_dir: Path, kb_root: Path, run_id: str | None) -> dict[str, Any]:
    """The run's own verdict: the synthesis gate, the sections, and the experiments the arbiter bought."""
    final = read_json(results_dir / "final-proposal.json")
    if final is None:
        return {"outcome": None, "reason": "no final-proposal.json: the run did not reach synthesis"}
    proposal = final.get("proposal") or {}
    gate = final.get("gate") or {}
    sections = {
        key: len((proposal.get(key) or {}).get("cites") or [])
        for key in (
            "mechanism",
            "therapeutic_hypothesis",
            "intervention",
            "stratification",
            "biomarker",
            "validation",
            "translation",
        )
        if isinstance(proposal.get(key), dict)
    }
    experiments = [r for r in read_jsonl(kb_root / "experiments.jsonl") if r.get("run_id") == run_id]
    decisions = [r for r in read_jsonl(kb_root / "decisions.jsonl") if r.get("run_id") == run_id]
    bought = sorted({e for d in decisions for e in (d.get("experiment_ids") or [])})
    return {
        "outcome": proposal.get("outcome"),
        "gate_passed": gate.get("passed"),
        "gate_unknown_ids": gate.get("unknown_ids") or {},
        "gate_cites_contradicted_or_contested": gate.get("cites_contradicted_or_contested") or {},
        "computed_claims_cited": gate.get("computed_claims_cited") or [],
        "failure_modes": len(proposal.get("failure_modes") or []),
        "sections": sections,
        "cites_total": sum(sections.values()),
        "experiments_proposed": len(experiments),
        "experiments_by_kind": dict(Counter(e.get("kind") for e in experiments)),
        "experiments_bought": len(bought),
        "decisions": [d.get("id") for d in decisions],
    }


def quote_outcomes_block(results_dir: Path) -> dict[str, Any]:
    """The per-stage quote-check reading, from the module that owns its definition.

    Imported inside the function because `evaluation.quote_outcomes` reads this module's own helpers, and a
    module-level import either way would be a cycle. The definition stays there; this file only persists it,
    which is what lets `evaluation/figures.py` keep its contract of drawing from the scorecard alone.
    """
    from .quote_outcomes import OutcomesInput, collect

    try:
        return collect(results_dir)
    except OutcomesInput as err:
        return {"reason": str(err)}


def mechanism_chain(results_dir: Path, kb_root: Path, calls: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Output A measured as a *shape*: do the claims the mechanism section cites form a chain?

    `pipeline/section_gates.py::check_mechanism` answers pass or fail. That is the verdict and not the
    measurement: a section citing nine edges that form one nine-step chain and a section citing two that
    happen to join both pass, and they are not the same result. This states the length instead, and names the
    cited causal claims that lie **off** the chain, which is where a reader should look — in an earlier run
    they were three `IL-5 -> motor neuron injury` edges the gate separately flagged as contradicted.

    Computed over the **cited** edges only, from `final-proposal.json` and `claims.jsonl`, not over the whole
    knowledge-base graph: what is being scored is what the proposal says, not what the base could have
    supported. `knowledge_base/status.py::mechanism_path` answers the other question, over typed records, and
    is the tool the agent is given; this works on the archived dicts so that a scorecard can be recomputed
    from an archive alone.
    """
    final = read_json(results_dir / "final-proposal.json")
    if final is None:
        return {"cited_causal": None, "reason": "no final-proposal.json: the run did not reach synthesis"}
    section = (final.get("proposal") or {}).get("mechanism")
    if not isinstance(section, dict):
        return {"cited_causal": None, "reason": "the proposal has no mechanism section"}

    claims = {c["id"]: c for c in read_jsonl(kb_root / "claims.jsonl") if c.get("id")}
    cited = [i for i in (section.get("cites") or []) if isinstance(i, str)]
    edges = [claims[i] for i in cited if i in claims and claims[i].get("cause_ref") and claims[i].get("effect_ref")]

    # Longest simple chain among the cited edges, by exhaustive walk: a mechanism section cites a handful of
    # claims, so the cost is irrelevant and the result is exact.
    outgoing: dict[str, list[dict[str, Any]]] = {}
    for edge in sorted(edges, key=lambda e: e["id"]):
        outgoing.setdefault(edge["cause_ref"], []).append(edge)
    best: list[dict[str, Any]] = []

    def walk(node: str, seen: set[str], path: list[dict[str, Any]]) -> None:
        nonlocal best
        if len(path) > len(best):
            best = list(path)
        for edge in outgoing.get(node, ()):
            if edge["effect_ref"] in seen:
                continue
            walk(edge["effect_ref"], seen | {edge["effect_ref"]}, path + [edge])

    for start in sorted(outgoing):
        walk(start, {start}, [])

    chain = [e["id"] for e in best]
    verified = [e["id"] for e in edges if e.get("provenance_status") == "verified"]
    names = {n["id"]: n.get("canonical_name") for n in read_jsonl(kb_root / "entities.jsonl") if n.get("id")}
    # The chain as a reader sees it, in order: what each step connects, who asserted it (`origin`) and whether
    # its quote was found in the stored source. A figure drawn from the scorecard needs the names, and the scorecard is
    # the only input a figure may read.
    steps = [
        {
            "claim_id": e["id"],
            "cause_ref": e["cause_ref"],
            "cause_name": names.get(e["cause_ref"]),
            "effect_ref": e["effect_ref"],
            "effect_name": names.get(e["effect_ref"]),
            "origin": e.get("origin"),
            "provenance_status": e.get("provenance_status"),
        }
        for e in best
    ]
    path_calls = Counter(c.get("phase") for c in calls if c.get("tool") == "mcp__kb__kb_mechanism_path")
    return {
        "cited": len(cited),
        "cited_causal": len(edges),
        "cited_verified": len(verified),
        "chain": chain,
        "chain_edges": len(chain),
        "chain_steps": steps,
        "chain_verified": [i for i in chain if i in verified],
        "off_chain": [e["id"] for e in edges if e["id"] not in set(chain)],
        "joins": len(chain) >= 2,
        "gate_requirement_met": len(chain) >= 2 and any(i in verified for i in chain),
        "path_tool_calls": dict(path_calls),
        "method": "longest simple chain among the causal claims the mechanism section cites "
        "(final-proposal.json + claims.jsonl); `gate_requirement_met` repeats the two conditions "
        "check_mechanism had before ADR 0033 (a join and one verified step), kept so that archives stay comparable; "
        "it is not the current gate",
    }


def quote_checks(calls: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """`check_quote`'s verdicts per phase, and the `exact` rate.

    The call *count* is not the measurement and reading it as one is a trap: fewer calls can come with a much
    higher `exact` rate and fewer `absent` verdicts (a quote that is not in the cited source), when the missing
    calls are the ones that used to fail. The rate is persisted because a number a pre-registration is judged
    against must come out of the archive.

    `result_preview` is truncated by the observer, so a verdict is read only when it survives the truncation;
    `unparsed` counts the rest rather than silently lowering the denominator. `unverifiable` (the checker could
    not make the check) is counted apart and left out of the rate, since it is not a judgement about the quote.
    """
    rows: dict[str, Counter] = {}
    for call in calls:
        if not str(call.get("tool") or "").endswith("check_quote"):
            continue
        phase = str(call.get("phase") or call.get("stage") or "unknown")
        match = re.search(r'"verdict"\s*:\s*"([a-z_]+)"', str(call.get("result_preview") or ""))
        status = re.search(r'"status"\s*:\s*"([a-z_]+)"', str(call.get("result_preview") or ""))
        key = (
            match.group(1)
            if match
            else ("unverifiable" if status and status.group(1) == "unverifiable" else "unparsed")
        )
        rows.setdefault(phase, Counter())[key] += 1
    out: dict[str, Any] = {}
    for phase, counter in rows.items():
        judged = sum(v for k, v in counter.items() if k not in ("unverifiable", "unparsed"))
        out[phase] = {
            "calls": sum(counter.values()),
            "verdicts": dict(counter),
            "judged": judged,
            "exact": counter.get("exact", 0),
            "exact_rate_of_judged": round(counter.get("exact", 0) / judged, 4) if judged else None,
            "absent": counter.get("absent", 0),
        }
    return out or {"reason": "no check_quote call in this archive"}


def paper_tool_use(calls: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Calls to the `paper` server per phase, and whether a served passage was outside the main text.

    The server is registered for `literature` and `generate` only, so *which* phase called it is the
    measurement, not the total. `outside_main_text` is read where the truncated preview still carries it, which
    it may not do at all; that is why `flag_unreadable` is reported rather than a zero that could be mistaken for a fact.
    """
    rows: dict[str, Counter] = {}
    for call in calls:
        tool = str(call.get("tool") or "")
        if "paper_navigate" not in tool and "paper_passage" not in tool:
            continue
        phase = str(call.get("phase") or call.get("stage") or "unknown")
        counter = rows.setdefault(phase, Counter())
        counter["paper_navigate" if "paper_navigate" in tool else "paper_passage"] += 1
        preview = str(call.get("result_preview") or "")
        if "paper_passage" in tool:
            if '"status":"refused"' in preview.replace(" ", "") or "too_wide" in preview:
                counter["refused"] += 1
            if "outside_main_text" in preview:
                counter[
                    (
                        "outside_main_text_true"
                        if re.search(r'"outside_main_text"\s*:\s*true', preview)
                        else "outside_main_text_false"
                    )
                ] += 1
            else:
                counter["flag_unreadable"] += 1
    return {phase: dict(counter) for phase, counter in rows.items()} or {"reason": "no paper tool call in this archive"}


def verify_verdicts(
    phase_reports: dict[str, dict[str, Any]], relations: dict[str, dict[str, Any]] | None = None
) -> dict[str, Any]:
    """What the verifier decided, and what became verified evidence.

    A verdict alone does not verify a relation: `Builder.confirmed` writes the verified copy only when the
    verdict is `confirmed` **and** `faithful` is true (the source claim says what its quote says "and no more").
    The two are counted apart here, per relation type, because the combination `confirmed` + unfaithful can
    account for every confirmed contradiction a run produces. Such a verdict writes a **narrowed** claim and a
    verified relation from it (`Builder.narrowed`), so this count means "confirmed on the quote with the claim
    overstated", not "discarded"; `not_narrowed` in the phase report names the ones refused. Archives written
    before `Builder.narrowed` existed recorded nothing for this combination.
    """
    report = phase_reports.get("verify")
    if report is None:
        return {"verdicts": None, "reason": "no verify report in this archive"}
    verdicts = {k: v for k, v in (report.get("verdicts") or {}).items() if isinstance(v, dict)}
    counts = Counter(v.get("verdict") if isinstance(v, dict) else v for v in (report.get("verdicts") or {}).values())
    data: dict[str, Any] = {"verdicts": sum(counts.values()), "counts": dict(counts)}
    if relations is None:
        return data | {"by_relation_type": None, "reason": "relation types not available"}
    by_type: dict[str, dict[str, int]] = {}
    for relation_id, verdict in verdicts.items():
        kind = (relations.get(relation_id) or {}).get("type") or "unknown"
        row = by_type.setdefault(kind, {"verdicts": 0, "confirmed_and_faithful": 0, "confirmed_not_faithful": 0})
        row["verdicts"] += 1
        if verdict.get("verdict") == "confirmed":
            row["confirmed_and_faithful" if verdict.get("faithful") else "confirmed_not_faithful"] += 1
    narrowed = [rid for rid, v in verdicts.items() if v.get("narrowed")]
    refused = report.get("not_narrowed") or {}
    # Proposed, refused and written are three different numbers; conflating the first with the last would report
    # every proposed narrowing as written even when the guards refused most of them.
    data |= {
        "narrowing": {
            "proposed": len(narrowed),
            "refused": len(refused),
            # Not "written": a restatement that passes the guards may also meet a claim the base
            # already holds, which writes nothing new (`seen_again`). The phase report does not
            # separate the two, so this says only that the guards did not refuse it.
            "not_refused": max(len(narrowed) - len(refused), 0),
            "refusal_reasons": sorted(refused.values()),
        }
    }
    return data | {
        "by_relation_type": by_type,
        "confirmed_discarded_as_unfaithful": sum(row["confirmed_not_faithful"] for row in by_type.values()),
    }


def candidate_classes(
    phase_reports: dict[str, dict[str, Any]], relations: dict[str, dict[str, Any]], claims: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """The verify pool split by what made each candidate checkable, with its unfaithful rate.

    The verify pool offers, besides relations whose source claim is already `verified`, those whose source merely
    **carries a quote** — claims whose faithfulness has not been judged yet. Whether that is worth doing is a
    question about the *share* and the rate, not the total, so both are reported per class.

    The claim statuses are read from the knowledge base as it stands, not as it stood during the run, so a
    source verified by a later run is classified as verified. Nothing in the pipeline re-verifies a source claim
    from a faithful confirmation, which is what makes the approximation tolerable: it is stated, not hidden.
    """
    report = phase_reports.get("verify")
    if report is None:
        return {"reason": "no verify report in this archive"}
    rows: dict[str, dict[str, int]] = {}
    for relation_id, verdict in (report.get("verdicts") or {}).items():
        if not isinstance(verdict, dict):
            continue
        source = claims.get((relations.get(relation_id) or {}).get("source_id") or "")
        if source is None:
            name = "source_is_not_a_claim"
        elif source.get("provenance_status") == "verified":
            name = "source_verified"
        elif source.get("quote"):
            name = "source_quoted_unverified"
        else:
            name = "source_neither"
        row = rows.setdefault(name, {"candidates": 0, "unfaithful": 0, "narrowed": 0})
        row["candidates"] += 1
        row["unfaithful"] += verdict.get("faithful") is False
        row["narrowed"] += bool(verdict.get("narrowed"))
    total = sum(row["candidates"] for row in rows.values())
    for row in rows.values():
        row["share_of_pool"] = round(row["candidates"] / total, 4) if total else None
        row["unfaithful_rate"] = round(row["unfaithful"] / row["candidates"], 4) if row["candidates"] else None
    return rows


def experiment_run(phase_reports: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Which bought experiments actually ran, and did the agents stay inside what each licenses?

    The chain the pipeline claims — an agent proposes a registered analysis, the arbiter buys it, the host
    executes it, an independent agent judges it — is not readable from a phase's cost or its agent count. These
    are the fields that say whether it happened, so a run that ends `not_executed` is scored as that rather than
    read by eye. Field names follow `Orchestrator._execute_phase` and `._reassess_phase`; `null` with a reason
    wherever the phase left no report.

    A run may execute several EXPs, so the measurement is a row per execution plus
    the counts. `eligible_bought` and `executed_count` are what DEL-12 is read against: a run that buys four
    eligible EXPs and executes one is not the same run as one that buys and executes one.
    """
    execute, reassess = phase_reports.get("execute"), phase_reports.get("reassess")
    if execute is None:
        return {"executed": None, "reason": "no execute report in this archive (the phase did not run)"}
    judgements = {entry.get("experiment_id"): entry for entry in (reassess or {}).get("judgements") or []}
    executions = []
    for execution in execute.get("executions") or []:
        check = execution.get("interpretation_check") or {}
        judgement = judgements.get(execution.get("experiment_id")) or {}
        verdict = judgement.get("judgement") or {}
        executions.append(
            {
                "experiment_id": execution.get("experiment_id"),
                "analysis_id": execution.get("analysis_id"),
                "hypothesis_id": execution.get("hypothesis_id"),
                "status": execution.get("status"),
                # The EXP whose computation this one shares, or null when it computed its own.
                "result_of": execution.get("result_of"),
                "not_executed_reason": execution.get("reason"),
                "tested_claims": execution.get("tested_claims") or [],
                "result_digest": execution.get("result_digest") or check.get("verified_result_digest"),
                "outcome": check.get("outcome"),
                "interpretation_faithful": check.get("faithful"),
                "interpretation_errors": check.get("errors") or [],
                "computed_claims_written": execution.get("written") or [],
                "computed_claims_seen_again": sorted((execution.get("seen_again") or {}).values()),
                # `decision` is the field `ReassessOutput` carries; previously this read `judgement` and
                # `verdict`, neither of which exists, so no scorecard ever recorded a reassessment verdict.
                "reassess_status": judgement.get("status"),
                "reassess_error": judgement.get("error"),
                "reassessment": verdict.get("decision"),
                "reassessment_claims": verdict.get("claim_ids") or [],
            }
        )
    row: dict[str, Any] = {
        "executed": execute.get("status") == "executed",
        "status": execute.get("status"),
        "not_executed_reason": execute.get("reason") if execute.get("status") != "executed" else None,
        "eligible_bought": len(execute.get("selected") or []),
        "executed_count": execute.get("executed_count"),
        # Absent in archives written before reuse existed, when every executed EXP ran its own computation: null, not zero.
        "reused_count": execute.get("reused_count"),
        "computations": len(
            {(e["analysis_id"], e["result_of"] or e["experiment_id"]) for e in executions if e["status"] == "executed"}
        ),
        "analyses_executed": sorted(
            {e["analysis_id"] for e in executions if e["status"] == "executed" and e["analysis_id"]}
        ),
        "executions": executions,
        # Every bought EXP the selector passed over keeps its reason: the run says what it could not run.
        "skipped_bought_experiments": execute.get("skipped") or [],
    }
    if reassess is None:
        return row | {"reassess_status": None, "judged_count": None}
    return row | {
        "reassess_status": reassess.get("status"),
        "reassess_reason": reassess.get("reason"),
        "judged_count": reassess.get("judged_count"),
    }


def score(results_dir: Path, kb_root: Path) -> dict[str, Any]:
    """Every measurement, from the archive. `null` with a reason wherever an input is missing."""
    from .tool_usage import read_calls

    state = read_json(results_dir / "state.json") or {}
    run_id = state.get("run_id")
    phase_reports = reports(results_dir)
    calls = read_calls(results_dir)
    relations = {r["id"]: r for r in read_jsonl(kb_root / "relations.jsonl") if r.get("id")}
    claims = {c["id"]: c for c in read_jsonl(kb_root / "claims.jsonl") if c.get("id")}
    completed = [p for p in (state.get("completed") or []) if isinstance(p, str)]
    return {
        "run_id": run_id,
        "kb": str(kb_root),
        "phases": {"completed": completed, "missing": [p for p in PHASE_ORDER if p not in completed]},
        "cost": cost(results_dir),
        "evidence_reach": evidence_reach(phase_reports, relations),
        **slice_and_citations(results_dir, phase_reports, relations),
        "search": search_use(calls),
        "losses": losses(phase_reports),
        "blinding": blinding(results_dir, calls),
        "filesystem": filesystem_reach(results_dir, calls),
        "verify": verify_verdicts(phase_reports, relations),
        "verify_candidate_classes": candidate_classes(phase_reports, relations, claims),
        "quote_checks": quote_checks(calls),
        # A second, different reading of the same phase: `quote_checks` is the truncated tool previews with
        # every refusal under `unverifiable`, which a pre-registered `exact` rate is judged against;
        # `quote_outcomes` is the complete server logs with the refusal causes split per stage, the host
        # verifier's own leg beside the agents', and the fetch attempts. Persisted here because
        # `evaluation/figures.py` draws only from this file, so a figure cannot disagree with it.
        "quote_outcomes": quote_outcomes_block(results_dir),
        "paper_tool": paper_tool_use(calls),
        "experiment": experiment_run(phase_reports),
        "mechanism_chain": mechanism_chain(results_dir, kb_root, calls),
        "result": outcome(results_dir, kb_root, run_id),
    }


def render(data: dict[str, Any]) -> str:
    reach, result = data["evidence_reach"], data["result"]
    search, losses_ = data["search"], data["losses"]
    lines = [
        f"# Run score — {data['run_id']}",
        "",
        f"- phases completed: {len(data['phases']['completed'])} of {len(PHASE_ORDER)}"
        + (f" (missing: {', '.join(data['phases']['missing'])})" if data["phases"]["missing"] else ""),
        f"- cost: {data['cost']['total_usd']} USD",
        f"- outcome: **{result.get('outcome')}**, gate passed: {result.get('gate_passed')}",
        "",
    ]
    lines += ["| measurement | value |", "|---|---|"]
    rows: list[tuple[str, Any]] = [
        ("fresh literature claims written by this run", reach.get("fresh_claims")),
        ("of those, linked by the hypothesis authors", reach.get("touched_by_authors")),
        ("of those, linked by any later phase", reach.get("touched_by_any_later_phase")),
    ]
    if data.get("slice"):
        s = data["slice"]
        rows.append(
            (
                "claims in the authors' slice (verified / fresh / computed)",
                f"{s['claims']} ({s['verified']} / {s['fresh']} / {s['computed']})",
            )
        )
    if data.get("citations"):
        c = data["citations"]
        rows += [
            ("claims the authors cited", c["claims_cited"]),
            ("of those, from neither slice nor prior graph (so from a tool)", c["from_neither_slice_nor_prior_graph"]),
        ]
    rows += [
        (
            "`kb_search` calls (with `types` / answered nothing)",
            f"{search['calls']} ({search['with_types']} / {search['empty']})",
        ),
        ("elements dropped by our validation", sum(losses_["dropped_elements"].values())),
        ("objections kept as questions instead of dropped", sum(losses_["converted_elements"].values())),
        ("whole agent outputs lost", sum(len(v) for v in losses_["dropped_agents"].values())),
        ("author-field leaks seen in the archive (a lower bound: previews are truncated)", data["blinding"]["leaks"]),
        (
            "filesystem calls outside the phase directory (expected 0; refused attempts and the CLI's own "
            "transcript tree apart)",
            f"{data['filesystem']['outside_succeeded']} ({data['filesystem']['outside_refused']} refused, "
            f"{data['filesystem']['transcript']} in the transcript tree)",
        ),
        ("computed claims cited in the proposal", len(result.get("computed_claims_cited") or [])),
        ("experiments proposed / bought", f"{result.get('experiments_proposed')} / {result.get('experiments_bought')}"),
        ("failure modes / citations in the proposal", f"{result.get('failure_modes')} / {result.get('cites_total')}"),
    ]
    chain = data.get("mechanism_chain") or {}
    if chain.get("cited_causal") is None:
        rows.append(("mechanism cited as a chain", f"not evaluable — {chain.get('reason', 'unknown')}"))
    else:
        rows += [
            (
                "longest chain among the causal claims section A cites (of how many cited)",
                f"{chain['chain_edges']} edges (of {chain['cited_causal']})",
            ),
            ("of those chain edges, verified", len(chain["chain_verified"])),
            ("causal claims cited off the chain", len(chain["off_chain"])),
            ("`kb_mechanism_path` calls, by phase", chain["path_tool_calls"] or "none"),
        ]
    exp = data.get("experiment") or {}
    if exp.get("executed") is not None:
        # One row per run, not per EXP: the per-execution detail is listed below the table. Reads the block
        # `experiment_run` writes one row per EXP; it used to read `selected_experiment_id`, a single-EXP field
        # that block no longer has, so rendering any archive scored after that change raised `KeyError`.
        rows.append(
            (
                "bought experiments executed (computations, reused, analyses)",
                (
                    (
                        f"{exp.get('executed_count')}, {exp.get('reused_count') if exp.get('reused_count') is not None else '—'}, "
                        f"{', '.join(exp.get('analyses_executed') or []) or '—'}"
                    )
                    if exp["executed"]
                    else f"no — {exp.get('not_executed_reason') or exp.get('status') or 'no reason recorded'}"
                ),
            )
        )
    lines += [f"| {name} | {value if value is not None else '—'} |" for name, value in rows]
    if data["verify"].get("counts"):
        lines += ["", f"Verifier verdicts: {data['verify']['counts']}."]
        by_type = data["verify"].get("by_relation_type")
        if by_type:
            for kind, row in sorted(by_type.items()):
                lines.append(
                    f"- `{kind}`: {row['verdicts']} verdicts, "
                    f"{row['confirmed_and_faithful']} confirmed and faithful (these became verified "
                    f"evidence), {row['confirmed_not_faithful']} confirmed but discarded as unfaithful."
                )
    if exp.get("reason"):
        lines += ["", f"No experiment phase in this archive: {exp['reason']}."]
    elif exp.get("executed"):
        lines.append("")
        for e in exp.get("executions") or []:
            shared = f" (the result of `{e['result_of']}`)" if e.get("result_of") else ""
            lines.append(
                f"- Experiment `{e['experiment_id']}` ran `{e['analysis_id']}`{shared} on hypothesis "
                f"`{e['hypothesis_id']}`: status `{e['status']}`, outcome `{e.get('outcome')}`, digest "
                f"`{(e.get('result_digest') or '')[:12]}…`, interpretation faithful: "
                f"{e.get('interpretation_faithful')}, reassessment: {e.get('reassess_status')} "
                f"{e.get('reassessment') or ''}".rstrip() + "."
            )
            if e.get("interpretation_errors"):
                lines.append(
                    f"  - **the agent's interpretation disagreed with the artifact**: " f"{e['interpretation_errors']}."
                )
        if exp.get("skipped_bought_experiments"):
            lines += [f"- bought experiments passed over: {exp['skipped_bought_experiments']}."]
    flagged = result.get("gate_cites_contradicted_or_contested") or {}
    if flagged:
        lines += ["", f"**The gate flagged citations of contradicted or contested claims**: {flagged}."]
    if data["blinding"]["leaks"]:
        lines += ["", f"**Author fields reached an agent**: {data['blinding']['where']}."]
    if data["filesystem"]["where"]:
        lines += ["", f"**Filesystem calls outside the phase directory**: {data['filesystem']['where']}."]
    for block in (reach, data.get("citations") or {}, result):
        if isinstance(block, dict) and block.get("reason"):
            lines += ["", f"Not measured: {block['reason']}."]
    return "\n".join(lines) + "\n"


def save(data: dict[str, Any], results_dir: Path) -> list[Path]:
    """Write the score beside the phase reports, as JSON and Markdown, and return the paths.

    Not under `results/analysis/`: that belongs to the `compute` phase, whose digest-verified artifacts are the
    *evidence* of a run, while this is a measurement *about* the run. Nothing is ever overwritten: a second scoring of the same archive — after the knowledge
    base has grown, or after the scorer itself changed — lands beside the first as `run-score.2.json`, so two
    readings can be compared instead of one silently replacing the other.
    """
    stem, attempt = "run-score", 1
    while (results_dir / f"{stem}.json").exists() or (results_dir / f"{stem}.md").exists():
        attempt += 1
        stem = f"run-score.{attempt}"
    written = []
    for path, text in (
        (results_dir / f"{stem}.json", json.dumps(data, indent=2) + "\n"),
        (results_dir / f"{stem}.md", render(data)),
    ):
        path.write_text(text)
        written.append(path)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("results_dir", type=Path, help="experiments/<RUN_ID>/results")
    parser.add_argument("--kb", type=Path, default=DEFAULT_KB, help=f"knowledge base (default {DEFAULT_KB})")
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    parser.add_argument(
        "--save", action="store_true", help="also write run-score.json and run-score.md into the run directory"
    )
    args = parser.parse_args(argv)
    if not args.results_dir.is_dir():
        print(f"run_score: no such directory: {args.results_dir}", file=sys.stderr)
        return 2
    data = score(args.results_dir, args.kb)
    print(render(data) if args.format == "markdown" else json.dumps(data, indent=2))
    if args.save:
        for path in save(data, args.results_dir):
            print(f"run_score: wrote {path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
