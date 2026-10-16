"""What `check_quote` said, per run and per pipeline stage, with the fetch attempts beside it.

    uv run python -m evaluation.quote_outcomes experiments/<RUN>/results [--format json|markdown]
    uv run python -m evaluation.quote_outcomes --runs RUN_A RUN_B [--experiments experiments]

Why this exists. An earlier run's `C-87` cited a university press page while its quote was a paper title; tracing
it (`evaluation/claim_trace.py`, bug observatory entry 11) showed the agent had found the real DOI and been
refused with `paper_not_in_store`. Whether that was one claim or a pattern had no tool behind it, and the
throwaway scripts that answered it got the headline wrong twice: they counted three different logs as one, and
they reported per run when the interesting axis is per **stage** — the literature store grows *during* a run, so
a refusal at `literature` and the same refusal at `synthesize` are different facts.

Three logs, three different things, and the error to avoid is adding them up:

- `literature-calls.jsonl` — the **host** verifier (`pipeline/provenance.py::QuoteVerifier`), during the run;
- `literature-calls.agents.jsonl` — the **agents**' own calls to the literature MCP server, during the run;
- `literature-calls.audit.jsonl` — `evaluation/citation_audit`'s re-checks, written **after** the run. Counted
  and reported apart, never inside the run's own figures.

Stage attribution differs by leg and each leg says which it is. The agents' stage is **recorded**: the tool log
(`*-agent-tools.jsonl`) carries it, and the `cause` survives that log's 200-byte truncation in every archived
case, so no join is needed. The host's log carries no phase at all, so its stage is **inferred from
timestamps** — the last stage that had started at or before the row's `ts`. That inference was checked against
three runs: the host's rows fall in the gaps *between* stage windows, which is where the orchestrator commits,
and they land on `literature`, `review` and `verify` only.

What it does **not** do. It does not re-check a quote (that is `evaluation/citation_audit`) and it does not
replace `run_score.quote_checks`, which reads the truncated tool previews, lumps every refusal under
`unverifiable`, and is what a pre-registered `exact` rate is judged against. This reads the complete server
logs, splits the refusal causes, adds the host leg, and puts the fetch attempts on the same stage axis. It
states no cause for any number.

Exit status: 0, 1 when no named archive yielded a measurement, 2 on an input error.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from pipeline.provenance import fetch_outcome
from tools.literature.store import canonical_id

from .run_score import PHASE_ORDER, read_json, read_jsonl, reports
from .tool_usage import read_calls

REPO_ROOT = Path(__file__).resolve().parents[1]

HOST_LOG = "literature-calls.jsonl"
AGENTS_LOG = "literature-calls.agents.jsonl"
AUDIT_LOG = "literature-calls.audit.jsonl"

# `tools/literature/mcp_server.py`: `status` is checked|unverifiable, `cause` is set only when unverifiable,
# `verdict` only when checked. One vocabulary over both, so "the checker declined" never reads as "the quote
# failed" — the distinction `run_score.quote_checks` and `figures.quote_exact_rate` already keep.
CAUSES = (
    "bad_input",
    "paper_not_in_store",
    "text_hash_mismatch",
    "text_not_available",
    "text_incomplete",
    "service_error",
)
VERDICTS = ("exact", "near", "absent")
UNPARSED = "unparsed"
OUTCOMES = (*(f"checked:{v}" for v in VERDICTS), *CAUSES, UNPARSED)

# `pipeline/orchestrator.py` writes `fetched` in the report of the phase that fetched.
FETCH_OUTCOMES = ("fetched", "paywalled", "unresolvable", "not_fetched", "failed", "skipped_over_cap")

HOST_RULE = (
    "the last stage that had started at or before the row's `ts`; the host's log carries no phase, and "
    "its rows fall in the gaps between stage windows, where the orchestrator commits"
)

LIMITS = {
    "legs_are_not_summed": "the host verifier and the agents are different callers; adding them double-counts "
    "nothing but means nothing either",
    "host_stage_is_inferred": HOST_RULE,
    "audit_rechecks_excluded": f"{AUDIT_LOG} is written by evaluation.citation_audit after the run, so its rows "
    f"are reported apart and are in no leg",
    "unparsed": "an outcome the tool log's truncation cut is counted as `unparsed`, never as a success",
    "fetch_reason": "an archive written before 2026-10-10 carries a bare fetch outcome, so its `paywalled` "
    "cannot be split between Europe PMC and OpenAlex; it is counted as unrecorded",
}


class OutcomesInput(ValueError):
    """The archive cannot be read as asked."""


# --- stages -------------------------------------------------------------------------------------------------


def phase_of(stage: str) -> str:
    """`05-review.attempt2` -> `review`. The stage keeps its own identity; this is only for ordering."""
    body = stage.split("-", 1)[1] if "-" in stage else stage
    return body.split(".", 1)[0]


def stage_windows(results_dir: Path) -> list[tuple[str, str]]:
    """Each stage and the timestamp of its first recorded tool call, earliest first.

    The tool log is the only artifact that times a stage: `state.json`'s launch record carries `wall_s` and
    `duration_ms` but no start, so a window has to come from the calls themselves.
    """
    first: dict[str, str] = {}
    for call in read_calls(results_dir):
        at = call.get("at")
        stage = call.get("stage")
        if isinstance(at, str) and isinstance(stage, str):
            if stage not in first or at < first[stage]:
                first[stage] = at
    return sorted(first.items(), key=lambda pair: pair[1])


def attribute(ts: str, windows: Sequence[tuple[str, str]]) -> str:
    """The stage a host-verifier row belongs to, by `HOST_RULE`. `before_any_stage` when it precedes them all."""
    chosen = None
    for stage, started in windows:
        if started[:19] <= ts[:19]:
            chosen = stage
        else:
            break
    return chosen or "before_any_stage"


def order_stages(stages: Iterable[str]) -> list[str]:
    """Pipeline order, an `.attemptN` after the phase it re-attempts, anything unrecognised last by name."""

    def key(stage: str) -> tuple[int, str]:
        phase = phase_of(stage)
        return (PHASE_ORDER.index(phase) if phase in PHASE_ORDER else len(PHASE_ORDER), stage)

    return sorted(set(stages), key=key)


# --- outcomes -----------------------------------------------------------------------------------------------


def outcome_of(row: Mapping[str, Any]) -> str:
    """One label per check, from a server-log row: the cause when it declined, the verdict when it judged."""
    if row.get("status") == "unverifiable":
        cause = row.get("cause")
        return cause if cause in CAUSES else UNPARSED
    verdict = row.get("verdict")
    return f"checked:{verdict}" if verdict in VERDICTS else UNPARSED


def outcome_of_preview(preview: str) -> str:
    """The same label out of a tool call's truncated `result_preview`.

    `status` and, when the check declined, `cause` both sit early enough in the answer to survive the 200-byte
    cut in every archived case; `unparsed` covers the rest rather than guessing a success.
    """
    status = re.search(r'"status"\s*:\s*"([a-z_]+)"', preview)
    if status and status.group(1) == "unverifiable":
        cause = re.search(r'"cause"\s*:\s*"([a-z_]+)"', preview)
        return cause.group(1) if cause and cause.group(1) in CAUSES else UNPARSED
    verdict = re.search(r'"verdict"\s*:\s*"([a-z_]+)"', preview)
    return f"checked:{verdict.group(1)}" if verdict and verdict.group(1) in VERDICTS else UNPARSED


def resolvable(row_or_arg: Any) -> bool:
    """Whether a refused check named a published identifier — a dead end that could have been closed."""
    return canonical_id(str(row_or_arg or "")) is not None


def state_of(path: Path, rows: Sequence[Any]) -> str:
    """`absent` when there is no such file, `empty` when there is one and it holds nothing, else `present`."""
    if not path.is_file():
        return "absent"
    return "present" if rows else "empty"


def _leg(
    by_stage: Mapping[str, Counter],
    refused_resolvable: Mapping[str, int],
    *,
    stage_source: str,
    sources: Sequence[str],
    rule: str | None = None,
) -> dict[str, Any]:
    totals: Counter = Counter()
    for counter in by_stage.values():
        totals.update(counter)
    leg: dict[str, Any] = {
        "stage_source": stage_source,
        "sources": list(sources),
        "by_stage": {stage: dict(by_stage[stage]) for stage in order_stages(by_stage)},
        "totals": dict(totals),
        "checks": sum(totals.values()),
        "paper_not_in_store_resolvable": {k: v for k, v in refused_resolvable.items() if v},
        "paper_not_in_store_resolvable_total": sum(refused_resolvable.values()),
    }
    if rule:
        leg["rule"] = rule
    return leg


# --- collection ---------------------------------------------------------------------------------------------


def agents_leg(results_dir: Path) -> dict[str, Any]:
    """The agents' checks, with the stage the tool log records and the outcome its preview carries.

    The server log is read too, and the two counts are reported side by side: they agreed exactly in every
    archive measured (760 against 760), so a disagreement is a finding about the archive, not a choice to make.
    """
    by_stage: dict[str, Counter] = {}
    refused: Counter = Counter()
    calls = [c for c in read_calls(results_dir) if str(c.get("tool") or "").endswith("check_quote")]
    for call in calls:
        stage = str(call.get("stage") or "unknown")
        outcome = outcome_of_preview(str(call.get("result_preview") or ""))
        by_stage.setdefault(stage, Counter())[outcome] += 1
        if outcome == "paper_not_in_store":
            paper = re.search(r'"paper"\s*:\s*"([^"]+)"', str(call.get("input_preview") or ""))
            if paper and resolvable(paper.group(1)):
                refused[stage] += 1
    server = [r for r in read_jsonl(results_dir / AGENTS_LOG) if r.get("quote")]
    leg = _leg(
        by_stage,
        refused,
        stage_source="recorded",
        sources=[f"*-agent-tools.jsonl ({len(calls)} check_quote calls)", f"{AGENTS_LOG} ({len(server)} rows)"],
    )
    leg["server_log_rows"] = len(server)
    if len(server) != len(calls):
        leg["disagreement"] = (
            f"{len(calls)} check_quote calls in the tool logs against {len(server)} rows in "
            f"{AGENTS_LOG}; neither is preferred here"
        )
    return leg


def host_leg(results_dir: Path) -> dict[str, Any]:
    """The orchestrator's own verifier, its stage inferred from the stage windows of the tool log."""
    windows = stage_windows(results_dir)
    by_stage: dict[str, Counter] = {}
    refused: Counter = Counter()
    for row in read_jsonl(results_dir / HOST_LOG):
        if not row.get("quote"):
            continue
        ts = row.get("ts")
        stage = attribute(str(ts), windows) if isinstance(ts, str) else "no_timestamp"
        outcome = outcome_of(row)
        by_stage.setdefault(stage, Counter())[outcome] += 1
        if outcome == "paper_not_in_store" and resolvable(row.get("paper_arg")):
            refused[stage] += 1
    return _leg(by_stage, refused, stage_source="inferred_from_timestamps", sources=[HOST_LOG], rule=HOST_RULE)


def fetches(results_dir: Path) -> dict[str, Any]:
    """The host's fetch attempts per phase, from each phase report's `fetched` map.

    Read beside the checks because together they separate "the store could not get the paper" from "nobody
    asked it to": a `paper_not_in_store` in a stage that attempted no fetch is the second of those.
    """
    by_stage: dict[str, Counter] = {}
    identifiers: dict[str, list[dict[str, str]]] = {}
    reasons: Counter = Counter()
    unrecorded = 0
    for phase, report in reports(results_dir).items():
        got = report.get("fetched")
        if not isinstance(got, dict) or not got:
            continue
        counter = by_stage.setdefault(phase, Counter())
        for ident, value in got.items():
            outcome, reason = fetch_outcome(value)
            counter[outcome] += 1
            if outcome != "fetched":
                identifiers.setdefault(phase, []).append(
                    {"id": str(ident), "outcome": outcome, "reason": reason or None}
                )
                # The reason is what separates "Europe PMC has no open-access text" from "and neither does
                # OpenAlex"; an archive written before it was kept counts as unrecorded, never as one of them.
                if reason:
                    reasons[reason] += 1
                else:
                    unrecorded += 1
    totals: Counter = Counter()
    for counter in by_stage.values():
        totals.update(counter)
    return {
        "by_stage": {s: dict(by_stage[s]) for s in order_stages(by_stage)},
        "totals": dict(totals),
        "attempts": sum(totals.values()),
        "not_fetched_identifiers": identifiers,
        "reasons": dict(reasons),
        "reason_unrecorded": unrecorded,
    }


# What became of a paper an agent had identified and could not read. Five fates, exhaustive by construction:
# the chain below ends in an `else`. Full sentences, because each one is a different thing to fix.
LATER = "arrived in the store later in the same run"
FETCHED_UNSEEN = "fetched by the host, but no later agent check saw it"
TRIED_FAILED = "the host tried and could not get it"
CITED_UNTRIED = "a claim cites it and no fetch was attempted"
NEVER_CITED = "never cited by a claim, so the host never had it as a fetch candidate"
FATES = (NEVER_CITED, LATER, CITED_UNTRIED, FETCHED_UNSEEN, TRIED_FAILED)


def _snapshots(results_dir: Path) -> list[tuple[str, set[str]]]:
    """What the store held at each stage's server start, as `(ts, every id and alias)`.

    The literature MCP server writes one `server_start` row per stage with the whole index
    (`tools/literature/mcp_server.py::_store_snapshot`), so "the paper arrived later in the run" is a fact read
    from the archive and not an inference from timing.
    """
    out = []
    for row in read_jsonl(results_dir / AGENTS_LOG):
        if row.get("event") != "server_start":
            continue
        ids: set[str] = set()
        for record in row.get("store_snapshot") or []:
            # `not_available` is a record saying the text could not be obtained (`store.add_unavailable`), so
            # the paper is *named* in the index and still unreadable. Counting it as held would report a
            # paywalled paper as one that "arrived later in the run", which is the opposite of what happened;
            # the throwaway script that first produced these numbers made exactly that mistake.
            if record.get("status") == "not_available":
                continue
            ids.add(str(record.get("canonical_id") or "").lower())
            ids |= {str(a).lower() for a in record.get("aliases") or []}
        out.append((str(row.get("ts") or ""), {i for i in ids if i}))
    return out


def _cited_identifiers(results_dir: Path) -> set[str] | None:
    """Every source reference of the run's own claims, canonicalised. `None` when the base cannot be read.

    The knowledge base is the one the run recorded in `context.json["kb"]`, as `claim_trace` reads it: only
    an earlier run and later have a base of their own. It matters because `pipeline.provenance.fetch_missing`
    runs over the sources of **committed claims** — a paper no claim cites is never a fetch candidate.
    """
    context = read_json(results_dir / "context.json") or {}
    kb = context.get("kb")
    if not kb:
        return None
    claims = Path(str(kb)) / "claims.jsonl"
    if not claims.is_file():
        return None
    out: set[str] = set()
    for claim in read_jsonl(claims):
        for ref in claim.get("source_refs") or []:
            out.add(canonical_id(str(ref)) or str(ref).lower())
    return out


def dead_ends(results_dir: Path) -> dict[str, Any]:
    """What became of each paper an agent identified and `check_quote` could not find.

    The measurement `fetch_paper` was built from: over 14 archives, 192 of the
    agents' 760 checks were refused `paper_not_in_store` on a resolvable DOI, PMID or PMCID — 151 distinct
    papers, of which 85 never reached a claim, 29 arrived later in the same run, and 16 were paywalled. It
    measures; it decides nothing, and it names no cause the archive does not carry.

    One trap, which the throwaway script that first produced these numbers fell into and this function does
    not: a `not_available` record (`store.add_unavailable`) puts the paper's identifier in the index while its
    text stays unreadable, so counting the index as "held" reported 20 paywalled papers as having *arrived*.
    """
    snapshots = _snapshots(results_dir)
    first_refusal: dict[str, str] = {}
    for row in read_jsonl(results_dir / AGENTS_LOG):
        if not row.get("quote") or outcome_of(row) != "paper_not_in_store":
            continue
        ident = canonical_id(row.get("paper_arg") or row.get("paper"))
        if ident is None:
            continue
        ts = str(row.get("ts") or "")
        first_refusal[ident] = min(first_refusal.get(ident, ts), ts)

    attempted: dict[str, str] = {}
    for report in reports(results_dir).values():
        for ident, value in (report.get("fetched") or {}).items():
            attempted[canonical_id(str(ident)) or str(ident).lower()] = fetch_outcome(value)[0]
    cited = _cited_identifiers(results_dir)

    rows: list[dict[str, Any]] = []
    counts: Counter = Counter()
    from_the_start = 0
    for ident, ts in sorted(first_refusal.items()):
        later = any(stamp > ts and ident in held for stamp, held in snapshots)
        tried = attempted.get(ident)
        if later:
            fate = LATER
        elif tried == "fetched":
            fate = FETCHED_UNSEEN
        elif tried is not None:
            fate = TRIED_FAILED
        elif cited is not None and ident in cited:
            fate = CITED_UNTRIED
        else:
            fate = NEVER_CITED
        counts[fate] += 1
        if not later and any(ident in held for _, held in snapshots):
            # It was already in the store when a stage started, so `paper_not_in_store` had another cause
            # (a text whose hash no longer matches, say). Counted apart, never used to contradict a fate.
            from_the_start += 1
        rows.append(
            {
                "identifier": ident,
                "first_refused_at": ts,
                "fate": fate,
                "host_fetch_outcome": tried,
                "cited_by_a_claim": None if cited is None else ident in cited,
            }
        )
    return {
        "identifiers": len(first_refusal),
        "by_fate": {f: counts[f] for f in FATES if counts[f]},
        "fates": rows,
        "in_the_store_from_the_start": from_the_start,
        "inputs": {
            AGENTS_LOG: state_of(results_dir / AGENTS_LOG, [r for r in read_jsonl(results_dir / AGENTS_LOG)]),
            "server_start snapshots": len(snapshots),
            "knowledge base": "absent" if cited is None else f"{len(cited)} cited identifiers",
        },
    }


def collect(results_dir: Path) -> dict[str, Any]:
    """Every quote-check outcome of one archive, per leg and per stage. Raises `OutcomesInput` if unreadable."""
    if not results_dir.is_dir():
        raise OutcomesInput(f"no such directory: {results_dir}")
    state = read_json(results_dir / "state.json") or {}
    host, agents = host_leg(results_dir), agents_leg(results_dir)
    audit = [r for r in read_jsonl(results_dir / AUDIT_LOG) if r.get("quote")]
    stages = order_stages([*host["by_stage"], *agents["by_stage"]])
    return {
        "run_id": state.get("run_id") or results_dir.parent.name,
        "archive": str(results_dir),
        "stages": stages,
        "legs": {"agents": agents, "host": host},
        "audit_rechecks": {
            "rows": len(audit),
            "outcomes": dict(Counter(outcome_of(r) for r in audit)),
            "note": "written by evaluation.citation_audit after the run, not by it",
        },
        "fetches": fetches(results_dir),
        "dead_ends": dead_ends(results_dir),
        "inputs": {
            name: state_of(results_dir / name, [r for r in read_jsonl(results_dir / name)])
            for name in (HOST_LOG, AGENTS_LOG, AUDIT_LOG)
        },
        "limits": dict(LIMITS),
    }


def aggregate(parts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Several archives, summed per leg and per stage. Runs stay named: a total with no runs behind it lies."""
    out: dict[str, Any] = {"runs": [p["run_id"] for p in parts], "legs": {}, "fetches": {}, "audit_rechecks": {}}
    for name in ("agents", "host"):
        by_stage: dict[str, Counter] = {}
        refused: Counter = Counter()
        for part in parts:
            leg = part["legs"][name]
            for stage, counts in leg["by_stage"].items():
                by_stage.setdefault(stage, Counter()).update(counts)
            refused.update(leg["paper_not_in_store_resolvable"])
        sample = parts[0]["legs"][name] if parts else {}
        # Not the sample's `sources`: those name one run's row counts, and printing them over a sum of
        # fourteen archives would be a number that belongs to the first of them.
        files = sorted({re.sub(r"\s*\(.*\)$", "", s) for p in parts for s in p["legs"][name]["sources"]})
        out["legs"][name] = _leg(
            by_stage,
            refused,
            stage_source=sample.get("stage_source", ""),
            sources=[f"{f}, over {len(parts)} archives" for f in files],
            rule=sample.get("rule"),
        )
    by_stage = {}
    for part in parts:
        for stage, counts in part["fetches"]["by_stage"].items():
            by_stage.setdefault(stage, Counter()).update(counts)
    totals: Counter = Counter()
    for counter in by_stage.values():
        totals.update(counter)
    reasons: Counter = Counter()
    for part in parts:
        reasons.update(part["fetches"].get("reasons") or {})
    out["fetches"] = {
        "by_stage": {s: dict(by_stage[s]) for s in order_stages(by_stage)},
        "totals": dict(totals),
        "attempts": sum(totals.values()),
        "reasons": dict(reasons),
        "reason_unrecorded": sum(p["fetches"].get("reason_unrecorded", 0) for p in parts),
    }
    rechecks: Counter = Counter()
    for part in parts:
        rechecks.update(part["audit_rechecks"]["outcomes"])
    out["audit_rechecks"] = {
        "rows": sum(p["audit_rechecks"]["rows"] for p in parts),
        "outcomes": dict(rechecks),
        "note": "written by evaluation.citation_audit after the runs, not by them",
    }
    out["stages"] = order_stages([*out["legs"]["agents"]["by_stage"], *out["legs"]["host"]["by_stage"]])
    # Dead ends are counted over **distinct identifiers**, so the aggregate unions them rather than adding the
    # per-run counts: the same paper refused in two runs is one paper the system could not read.
    fates: dict[str, str] = {}
    rows: list[dict[str, Any]] = []
    for part in parts:
        for row in part["dead_ends"]["fates"]:
            rows.append({**row, "run_id": part["run_id"], "archive": part["archive"]})
            # Keyed on the **archive**, not the run id: `pl-replay-007-reassess` records `run_id`
            # An earlier run, and keying on that silently merged its 12 dead ends into the other run's.
            fates[f"{part['archive']}/{row['identifier']}"] = row["fate"]
    counted: Counter = Counter(fates.values())
    out["dead_ends"] = {
        "identifiers": len(fates),
        "by_fate": {f: counted[f] for f in FATES if counted[f]},
        "fates": rows,
        "in_the_store_from_the_start": sum(p["dead_ends"]["in_the_store_from_the_start"] for p in parts),
        "note": "counted per run and per identifier: the same paper refused in two runs is two "
        "dead ends, because each run had to read it and could not",
    }
    out["limits"] = dict(LIMITS)
    return out


# --- rendering ----------------------------------------------------------------------------------------------


def _row(label: str, counts: Mapping[str, int], columns: Sequence[str]) -> str:
    cells = " | ".join(str(counts.get(c, "—")) for c in columns)
    return f"| {label} | {cells} | {sum(counts.values())} |"


def _table(title: str, by_stage: Mapping[str, Mapping[str, int]], columns: Sequence[str]) -> list[str]:
    if not by_stage:
        return [f"**{title}** — no checks recorded.", ""]
    head = " | ".join(f"`{c}`" for c in columns)
    out = [f"**{title}**", "", f"| stage | {head} | total |", "|---|" + "---:|" * (len(columns) + 1)]
    out += [_row(stage, by_stage[stage], columns) for stage in by_stage]
    return out + [""]


def render(data: Mapping[str, Any]) -> str:
    runs = data.get("runs") or [data.get("run_id")]
    out = [f"# Quote-check outcomes — {', '.join(str(r) for r in runs)}", ""]
    for name, title in (("agents", "The agents' own checks"), ("host", "The host verifier's checks")):
        leg = data["legs"][name]
        columns = [c for c in OUTCOMES if leg["totals"].get(c)]
        out += [
            f"## {title}",
            "",
            f"- stage: **{leg['stage_source']}**" + (f" — {leg['rule']}" if leg.get("rule") else ""),
            f"- read from: {', '.join(leg['sources'])}",
        ]
        if leg.get("disagreement"):
            out.append(f"- **disagreement**: {leg['disagreement']}")
        out.append("")
        out += _table("Outcomes by stage", leg["by_stage"], columns)
        refused = leg["paper_not_in_store_resolvable"]
        out += [
            f"Of the {leg['totals'].get('paper_not_in_store', 0)} refused for `paper_not_in_store`, "
            f"**{leg['paper_not_in_store_resolvable_total']} named a resolvable identifier** "
            f"(DOI, PMID or PMCID)" + (f": {json.dumps(refused)}" if refused else "") + ".",
            "",
        ]

    fetch = data["fetches"]
    out += ["## The host's fetch attempts", ""]
    out += _table("Fetch outcomes by stage", fetch["by_stage"], [c for c in FETCH_OUTCOMES if fetch["totals"].get(c)])
    out.append(
        f"{fetch['attempts']} attempts in all, of which {fetch['totals'].get('fetched', 0)} succeeded. "
        f"A stage absent from this table attempted none."
    )
    if fetch.get("reasons"):
        out += ["", "Why the rest did not arrive, as the fetcher recorded it:", ""]
        out += [f"- {count} × {reason}" for reason, count in sorted(fetch["reasons"].items(), key=lambda kv: -kv[1])]
    if fetch.get("reason_unrecorded"):
        out += [
            "",
            f"{fetch['reason_unrecorded']} attempt(s) carry no reason: written before the archive kept "
            f"one, so `paywalled` there cannot be split between Europe PMC and OpenAlex.",
        ]
    dead = data.get("dead_ends") or {}
    if dead.get("identifiers"):
        out += [
            "",
            "## What became of the papers an agent found and could not read",
            "",
            f"{dead['identifiers']} identifier(s) the agents named and `check_quote` refused with "
            f"`paper_not_in_store`, each resolvable to a DOI, PMID or PMCID:",
            "",
            "| n | fate |",
            "|---:|---|",
        ]
        out += [f"| {n} | {fate} |" for fate, n in sorted(dead["by_fate"].items(), key=lambda kv: -kv[1])]
        out += [
            "",
            "A fate is what the archive shows, not a cause. `arrived in the store later in the same "
            "run` is read from the per-stage `server_start` snapshots; `never cited by a claim` is why "
            "the host's own fetch never considered it, since it runs over committed claims only.",
        ]
        if dead.get("in_the_store_from_the_start"):
            out += [
                "",
                f"{dead['in_the_store_from_the_start']} of them were in the store when a stage started, "
                f"so that refusal had another cause (an unreadable or changed text, say).",
            ]
        if dead.get("note"):
            out += ["", dead["note"] + "."]
        out.append("")
    audit = data["audit_rechecks"]
    out += [
        "",
        "## Not part of any leg",
        "",
        f"- `{AUDIT_LOG}`: {audit['rows']} rows — {audit['note']}.",
        "",
        "## Limits",
        "",
    ]
    out += [f"- {text}" for text in data["limits"].values()]
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("results_dir", nargs="?", type=Path, help="experiments/<RUN_ID>/results")
    parser.add_argument("--runs", nargs="+", default=None, help="run ids to aggregate, instead of one archive")
    parser.add_argument("--experiments", type=Path, default=REPO_ROOT / "experiments")
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    args = parser.parse_args(argv)
    if (args.results_dir is None) == (args.runs is None):
        print("quote_outcomes: give one results directory, or --runs with run ids", file=sys.stderr)
        return 2

    if args.results_dir is not None:
        try:
            data = collect(args.results_dir)
        except OutcomesInput as err:
            print(f"quote_outcomes: {err}", file=sys.stderr)
            return 2
    else:
        parts = []
        for run_id in args.runs:
            try:
                parts.append(collect(args.experiments / run_id / "results"))
            except OutcomesInput as err:
                print(f"{run_id}: skipped — {err}", file=sys.stderr)
        if not parts:
            print("quote_outcomes: no named archive could be read", file=sys.stderr)
            return 1
        data = aggregate(parts)
    print(json.dumps(data, indent=2) if args.format == "json" else render(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
