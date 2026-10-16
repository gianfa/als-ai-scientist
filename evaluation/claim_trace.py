"""The archive's record for one claim: who wrote it, with which tools, and what the quote check said.

    uv run python -m evaluation.claim_trace experiments/<RUN>/results C-87 [--kb <dir>] [--format json]

Why this exists. An earlier run's synthesis gate reported `cites_an_unresolvable_source` naming
`C-87` in three sections. Diagnosing that one claim took five separate file reads by hand, and produced one
wrong answer on the way: the claim's `origin: "literature"` was read as if it named the phase that wrote it,
when the writer was a `review` agent. The finished diagnosis was worth having — the claim's `quote` is a paper
title while its only `source_refs` entry is a university press page, and the agent had reached the real PNAS
DOI and been refused by `check_quote` with `paper_not_in_store`. It is bug observatory entry 11. This module
does those five reads, so the next flagged claim costs a minute and the same mistake cannot be made twice.

What it does **not** do. It draws no conclusion about whether an agent read a quote or wrote it from memory,
and it never prints such a label. That is not caution, it is what the archive supports: `input_preview` is cut
at `INPUT_PREVIEW` bytes and `result_preview` at `RESULT_PREVIEW` (`pipeline/extract_workflow_result.py`, whose
constants this module imports rather than restating, so the stated limit cannot drift from the code that cuts);
agent transcripts are not archived; and an agent can read its own transcript tree, which this log does not
cover (`run_score.filesystem_reach`'s `transcript` bucket). So "no tool call recorded for this quote" is not
"the quote was invented". The posture is `filesystem_reach`'s — a lower bound — and `tool_usage.diagnose`'s:
plain signals, no interpretation.

It is read-only and offline. Unlike `citation_audit`, it re-checks nothing and writes nothing: it reports the
checks the **run itself** made, out of the run's own logs.

Three joins in here are easy to get backwards, and each has a test of its own:

1. `kb/events.jsonl` is the only carrier of record -> agent: `Claim` and `Relation` hold no `agent_id`. The
   key is `events.agent_id` (e.g. `reviewer-2@H-3`) against the tool log's **`label`**; the tool log's own
   `agent_id` is an opaque hash and is not the key.
2. A claim's `origin` (`literature | agent | computation | experiment`) is **not** a phase. The phase comes
   from the phase report whose `written` list holds the ID.
3. A claim ID is not unique across knowledge bases — the shared `knowledgebase/` holds a different `C-87`,
   written by an earlier run. The claim's own `run_id` is compared with the archive's and a mismatch is stated
   before anything else.

Exit status: 0 traced, 1 the claim is not in that knowledge base (naming the ID the store kept, when it was
dropped as a duplicate), 2 input error.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from knowledge_base.schemas import Claim, Event, normalize
from knowledge_base.store import read_records
from knowledge_base.validate import Record
from pipeline.extract_workflow_result import INPUT_PREVIEW, RESULT_PREVIEW
from tools.literature.store import canonical_id, unresolved_refs

from .citation_audit import at_run_end, carrying_sentences, cites, rests_on
from .quote_outcomes import AUDIT_LOG
from .run_score import DEFAULT_KB, quote_checks, read_json, read_jsonl, reports, written_ids
from .tool_usage import read_calls

CLAIM_FIELDS = (
    "content",
    "quote",
    "source_refs",
    "source_refs_raw",
    "origin",
    "provenance_status",
    "verification_method",
    "context",
    "mechanism",
    "cause_ref",
    "effect_ref",
    "sign",
    "type",
    "canonical_hash",
)

ORIGIN_IS_NOT_A_PHASE = (
    "`origin` says what kind of thing the claim came from, not which phase wrote it; "
    "the writing phase is in 'who wrote it'."
)

NO_VERDICT = (
    "This report states what the archive records. It derives no label from absence: a quote with no "
    f"tool call recorded for it was not shown to be invented. Tool inputs are truncated at "
    f"{INPUT_PREVIEW} bytes and results at {RESULT_PREVIEW} (pipeline/extract_workflow_result.py), "
    "agent transcripts are not archived, and an agent's own transcript tree is outside this log."
)


class TraceInput(ValueError):
    """The archive or the knowledge base cannot be read as asked."""


# --- inputs -------------------------------------------------------------------------------------------------


def resolve_kb(results_dir: Path, given: Path | None) -> tuple[Path, str]:
    """The knowledge base this archive belongs to, and why that one.

    Every archive records its own base in `context.json["kb"]` and nothing read it until now, which matters:
    later runs have a base of their own and every earlier run wrote into the shared `knowledgebase/`.
    """
    if given is not None:
        return given, "given with --kb"
    context = read_json(results_dir / "context.json") or {}
    recorded = context.get("kb")
    if isinstance(recorded, str) and recorded and Path(recorded).is_dir():
        return Path(recorded), 'recorded by the run in context.json["kb"]'
    beside = results_dir.parent / "kb"
    if beside.is_dir():
        return beside, "the kb/ directory beside this archive"
    return DEFAULT_KB, "the shared knowledgebase/, this archive naming no base of its own"


def found_rows(path: Path, rows: list[dict[str, Any]], matched: list[Any]) -> dict[str, Any]:
    """How an input read: absent, present and empty, or present with a stated number of matches.

    Three states, never collapsed into one. A missing file and a file with no matching row are different facts
    and the difference is the whole value of the section (the provenance requirement: a
    measurement whose input is missing is reported as missing, never as zero).
    """
    if not path.is_file():
        return {"file": str(path.name), "state": "absent", "rows": None, "matched": 0}
    return {"file": str(path.name), "state": "present" if rows else "empty", "rows": len(rows), "matched": len(matched)}


# --- the five sections --------------------------------------------------------------------------------------


def the_claim(record: Record, records: Mapping[str, Record], run_id: str | None) -> dict[str, Any]:
    """The record as written, with its epistemic status now and as the run left it."""
    dumped = record.model_dump(mode="json")
    fields = {k: dumped[k] for k in CLAIM_FIELDS if k in dumped}
    out: dict[str, Any] = {
        "id": record.id,
        "kind": type(record).__name__,
        "run_id": record.run_id,
        "created_at": dumped.get("created_at"),
        "fields": fields,
        "note": ORIGIN_IS_NOT_A_PHASE,
    }
    if run_id:
        out["status_at_run_end"] = at_run_end(records, run_id).get(record.id)
    if isinstance(record, Claim):
        # The gate's own function, so the report explains the flag in the gate's terms and decides nothing.
        out["unresolved_source_refs"] = unresolved_refs(record.source_refs)
    else:
        out["rests_on"] = rests_on(record, records)
    return out


def who_wrote_it(
    claim_id: str,
    records: Mapping[str, Record],
    phase_reports: Mapping[str, dict[str, Any]],
    calls: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """The write events, and the phase derived from three independent sources with any disagreement stated."""
    events = [
        {
            "event_id": e.id,
            "event": e.event,
            "agent_id": e.agent_id,
            "run_id": e.run_id,
            "at": e.created_at.isoformat(),
            "detail": e.detail,
        }
        for e in records.values()
        if isinstance(e, Event) and e.record_id == claim_id
    ]
    events.sort(key=lambda row: row["at"])
    agents = [row["agent_id"] for row in events if row["event"] == "written" and row["agent_id"]]

    from_report = sorted(
        phase
        for phase, report in phase_reports.items()
        if claim_id in written_ids(report, claim_id.split("-", 1)[0] + "-")
    )
    from_summaries = sorted(
        {
            phase
            for phase, report in phase_reports.items()
            for key in (report.get("summaries") or {})
            if any(str(key).endswith(agent) for agent in agents)
        }
    )
    from_tool_log = sorted({str(call.get("stage") or "") for call in calls if str(call.get("label") or "") in agents})
    out = {
        "events": events,
        "agents": agents,
        "phase_by_written_list": from_report,
        "phase_by_report_summaries": from_summaries,
        "stage_by_tool_log": from_tool_log,
    }
    named = {p for p in from_report} | {p for p in from_summaries}
    stages = {s.split("-", 1)[1] if "-" in s else s for s in from_tool_log}
    if named and stages and not (named & stages):
        out["disagreement"] = (
            f"the phase report says {sorted(named)} and the tool log says "
            f"{sorted(from_tool_log)}; neither is preferred here"
        )
    return out


def tools_called(agents: Iterable[str], calls: list[dict[str, Any]], results_dir: Path) -> dict[str, Any]:
    """Every recorded call of the agents that wrote the record, joined on the tool log's `label`."""
    wanted = {a for a in agents if a}
    mine = [call for call in calls if str(call.get("label") or "") in wanted]
    mine.sort(key=lambda call: (str(call.get("stage") or ""), call.get("ordinal") or 0))
    rows = [
        {
            "stage": call.get("stage"),
            "ordinal": call.get("ordinal"),
            "tool": call.get("tool"),
            "at": call.get("at"),
            "status": call.get("status"),
            "result_bytes": call.get("result_bytes"),
            "input_preview": call.get("input_preview"),
            "result_preview": call.get("result_preview"),
            "input_truncated": len(str(call.get("input_preview") or "")) >= INPUT_PREVIEW,
            "result_truncated": isinstance(call.get("result_bytes"), int) and call["result_bytes"] > RESULT_PREVIEW,
        }
        for call in mine
    ]
    logs = sorted(p.name for p in results_dir.glob("*-agent-tools.jsonl"))
    out: dict[str, Any] = {
        "join": "events.agent_id == the tool log's `label`; the log's own `agent_id` is an opaque hash",
        "logs": logs,
        "calls_in_archive": len(calls),
        "calls": rows,
        "quote_checks": quote_checks(mine),
    }
    if not logs:
        out["state"] = "absent"
        out["reason"] = "no *-agent-tools.jsonl in this archive"
    elif not calls:
        out["state"] = "empty"
        out["reason"] = f"{len(logs)} log file(s), no rows"
    elif not rows:
        out["state"] = "no_match"
        out["reason"] = (
            f"{len(calls)} calls recorded, none by {sorted(wanted)}; labels present: "
            f"{sorted({str(c.get('label')) for c in calls})}"
        )
    else:
        out["state"] = "present"
    return out


def quote_attempts(record: Record, results_dir: Path) -> dict[str, Any]:
    """The `check_quote` attempts for this claim, each row naming the log it came from and the rule that found it.

    Three logs, and **two of them are the run and one is not**. `literature-calls.jsonl` is the orchestrator's
    own verifier and `literature-calls.agents.jsonl` is the server the agents call: both are the run.
    `literature-calls.audit.jsonl` is `evaluation/citation_audit` re-checking the same quote **after** the run,
    against whatever the store holds today, so a row of it says nothing about what the run was able to see.
    Every row carries `during_the_run`, and the rows of the third log are reported apart.
    """
    quote = getattr(record, "quote", None)
    refs = {canonical_id(r) for r in getattr(record, "source_refs", [])} | set(getattr(record, "source_refs", []))
    refs.discard(None)
    files: list[dict[str, Any]] = []
    matches: list[dict[str, Any]] = []
    for path in sorted(results_dir.glob("literature-calls*.jsonl")):
        rows = [r for r in read_jsonl(path) if not r.get("event")]  # skip `server_start` lifecycle rows
        here = []
        for row in rows:
            rule = match_rule(row, quote, refs)
            if rule:
                here.append(
                    {
                        "log": path.name,
                        "matched_by": rule,
                        "during_the_run": path.name != AUDIT_LOG,
                        **{
                            k: row.get(k)
                            for k in (
                                "ts",
                                "paper_arg",
                                "paper",
                                "quote",
                                "status",
                                "cause",
                                "verdict",
                                "assessment",
                                "receipt_id",
                                "near_min",
                            )
                        },
                    }
                )
        files.append(found_rows(path, rows, here))
        matches.extend(here)
    return {
        "logs": files,
        "attempts": [m for m in matches if m["during_the_run"]],
        "audit_rechecks": [m for m in matches if not m["during_the_run"]],
        "searched_for": {"quote": quote, "source_refs": sorted(str(r) for r in refs)},
    }


def match_rule(row: Mapping[str, Any], quote: str | None, refs: set[Any]) -> str | None:
    """Which stated rule links a quote-check row to this claim, or None. Rules are tried in this order."""
    logged = str(row.get("quote") or "")
    if quote and logged == quote:
        return "quote, exactly"
    if quote and logged:
        a, b = normalize(quote), normalize(logged)
        if a and b and (a in b or b in a):
            return "quote, normalised containment"
    for field in ("paper", "paper_arg"):
        value = row.get(field)
        if value and (value in refs or canonical_id(str(value)) in refs):
            return f"{field} equals a source_ref"
    return None


def where_cited(claim_id: str, results_dir: Path) -> dict[str, Any]:
    """The proposal's sections and gate keys that name the record, and the sentences carrying its inline tag."""
    path = results_dir / "final-proposal.json"
    final = read_json(path)
    if final is None:
        return {
            "file": path.name,
            "state": "absent",
            "reason": "no final-proposal.json: the run did not reach synthesis, or did not finish it",
        }
    proposal = final.get("proposal") or {}
    sections = sorted({section for section, cited in cites(proposal) if cited == claim_id})
    gate = final.get("gate") or {}
    flags: dict[str, Any] = {}
    for key, value in gate.items():
        if isinstance(value, dict):
            # The gate writes "C-87 (https://news.uzh.ch/...)", so compare the first token, as
            # `citation_audit.weaknesses` does.
            named = {
                s: [v for v in listed if str(v).split()[0] == claim_id]
                for s, listed in value.items()
                if isinstance(listed, list)
            }
            if any(named.values()):
                flags[key] = {s: v for s, v in named.items() if v}
        elif isinstance(value, list) and any(str(v).split()[0] == claim_id for v in value):
            flags[key] = [v for v in value if str(v).split()[0] == claim_id]
    return {
        "file": path.name,
        "state": "present",
        "cited_in_sections": sections,
        "gate_keys_naming_it": flags,
        "sentences": carrying_sentences(proposal, claim_id),
    }


# --- the trace ----------------------------------------------------------------------------------------------


def dropped_to(claim_id: str, phase_reports: Mapping[str, dict[str, Any]]) -> tuple[str, str] | None:
    """The ID the store kept instead of this one, when a phase reported it as `seen_again`."""
    for phase, report in phase_reports.items():
        kept = (report.get("seen_again") or {}).get(claim_id)
        if isinstance(kept, str):
            return kept, phase
    return None


def trace(results_dir: Path, claim_id: str, kb: Path | None = None) -> dict[str, Any]:
    """Everything the archive records about one record. Raises `TraceInput` when it cannot be assembled."""
    if not results_dir.is_dir():
        raise TraceInput(f"no such directory: {results_dir}")
    kb_root, why = resolve_kb(results_dir, kb)
    if not kb_root.is_dir():
        raise TraceInput(f"no such knowledge base: {kb_root} ({why})")
    records = read_records(kb_root)
    state = read_json(results_dir / "state.json") or {}
    archive_run = state.get("run_id")
    phase_reports = reports(results_dir)

    record = records.get(claim_id)
    if record is None:
        if redirect := dropped_to(claim_id, phase_reports):
            kept, phase = redirect
            raise TraceInput(
                f"{claim_id} is not in {kb_root}: the store dropped it as a duplicate and kept "
                f"{kept} ({phase} phase, seen_again) — re-run on {kept}"
            )
        raise TraceInput(f"{claim_id} is not in {kb_root} ({why})")

    out: dict[str, Any] = {
        "claim_id": claim_id,
        "archive": str(results_dir),
        "archive_run_id": archive_run,
        "kb_root": str(kb_root),
        "kb_chosen_because": why,
    }
    if archive_run and record.run_id != archive_run:
        out["other_run"] = (
            f"{claim_id} in this knowledge base was written by run {record.run_id}, not by this "
            f"archive's {archive_run}: a claim ID is not unique across knowledge bases"
        )

    calls = read_calls(results_dir)
    out["the_claim"] = the_claim(record, records, archive_run)
    out["who_wrote_it"] = who_wrote_it(claim_id, records, phase_reports, calls)
    out["tools_called"] = tools_called(out["who_wrote_it"]["agents"], calls, results_dir)
    out["quote_attempts"] = quote_attempts(record, results_dir)
    out["where_cited"] = where_cited(claim_id, results_dir)
    out["limits"] = {
        "input_preview_bytes": INPUT_PREVIEW,
        "result_preview_bytes": RESULT_PREVIEW,
        "no_verdict": NO_VERDICT,
    }
    return out


# --- rendering ----------------------------------------------------------------------------------------------


def render(data: Mapping[str, Any]) -> str:
    out: list[str] = [f"# {data['claim_id']} in {Path(str(data['archive'])).parent.name}", ""]
    out += [
        f"- knowledge base: `{data['kb_root']}` ({data['kb_chosen_because']})",
        f"- archive run: `{data.get('archive_run_id')}`",
        "",
    ]
    if other := data.get("other_run"):
        out += [f"**{other}**", ""]

    claim = data["the_claim"]
    out += [
        f"## 1. The {claim['kind'].lower()} as written",
        "",
        f"Written in run `{claim['run_id']}` at {claim['created_at']}.",
    ]
    if claim.get("status_at_run_end"):
        out.append(f"Epistemic status as the run left it: **{claim['status_at_run_end']}**.")
    out.append("")
    for key, value in claim["fields"].items():
        out.append(f"- `{key}`: {_short(value)}")
    if claim.get("unresolved_source_refs"):
        out += [
            "",
            f"`unresolved_refs` (the synthesis gate's own function) returns "
            f"{claim['unresolved_source_refs']} — neither a published identifier nor an artifact of "
            f"this system.",
        ]
    if claim.get("rests_on"):
        out += ["", f"Rests on: {claim['rests_on']}"]
    out += ["", f"_{claim['note']}_", ""]

    wrote = data["who_wrote_it"]
    out += ["## 2. Who wrote it, and in which phase", ""]
    for event in wrote["events"]:
        out.append(
            f"- `{event['event_id']}` **{event['event']}** by `{event['agent_id']}` "
            f"in run `{event['run_id']}` at {event['at']}" + (f" — {event['detail']}" if event.get("detail") else "")
        )
    if not wrote["events"]:
        out.append("- no event in this knowledge base names this record")
    out += [
        "",
        f"- phase, by the report whose `written` list holds it: {wrote['phase_by_written_list'] or '—'}",
        f"- phase, by the report's `summaries` keys: {wrote['phase_by_report_summaries'] or '—'}",
        f"- stage, by the tool log carrying the label: {wrote['stage_by_tool_log'] or '—'}",
    ]
    if disagreement := wrote.get("disagreement"):
        out.append(f"- **disagreement**: {disagreement}")
    out.append("")

    called = data["tools_called"]
    out += ["## 3. Which tools that agent called", "", f"_{called['join']}._", ""]
    if called["state"] != "present":
        out += [f"**{called['state']}**: {called['reason']}", ""]
    for call in called["calls"]:
        marks = "".join(["T" if call["input_truncated"] else "", "t" if call["result_truncated"] else ""])
        out.append(
            f"- `{call['stage']}` #{call['ordinal']} **{call['tool']}** [{call['status']}, "
            f"{call['result_bytes']} B{', truncated ' + marks if marks else ''}]"
        )
        out.append(f"  - in: `{call['input_preview']}`")
        out.append(f"  - out: `{call['result_preview']}`")
    if called["quote_checks"]:
        out += ["", f"`check_quote` verdicts by phase: {json.dumps(called['quote_checks'])}"]
    out.append("")

    attempts = data["quote_attempts"]
    out += ["## 4. What the run's quote check said", ""]
    for log in attempts["logs"]:
        out.append(f"- `{log['file']}`: {log['state']}, {log['rows']} rows, {log['matched']} matching")
    out.append("")
    for row in attempts["attempts"]:
        out.append(
            f"- `{row['log']}` ({row['matched_by']}): **{row['status']}**"
            f"{', cause `' + str(row['cause']) + '`' if row.get('cause') else ''}"
            f"{', verdict `' + str(row['verdict']) + '`' if row.get('verdict') else ''}"
        )
        out.append(f"  - asked for: `{row['paper_arg']}` (resolved: `{row['paper']}`)")
    if not attempts["attempts"]:
        out.append("- no row of the run's own logs matches this claim's quote or source references")
    for row in attempts.get("audit_rechecks") or []:
        out.append(
            f"- `{row['log']}` ({row['matched_by']}): **{row['status']}** — a `citation_audit` re-check "
            f"made AFTER the run, against the store as it stands today, not what the run could see"
        )
    out.append("")

    cited = data["where_cited"]
    out += ["## 5. Where it is cited", ""]
    if cited["state"] == "absent":
        out += [f"- `{cited['file']}`: absent — {cited['reason']}", ""]
    else:
        out.append(f"- sections citing it: {cited['cited_in_sections'] or '—'}")
        out.append(f"- gate keys naming it: {json.dumps(cited['gate_keys_naming_it']) or '—'}")
        for sentence in cited["sentences"]:
            out.append(f"- `{sentence['at']}`: {sentence['sentence']}")
        out.append("")

    out += ["## Limits", "", data["limits"]["no_verdict"], ""]
    return "\n".join(out)


def _short(value: Any, limit: int = 400) -> str:
    text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    return text if len(text) <= limit else text[:limit] + " […]"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("results_dir", type=Path, help="experiments/<RUN_ID>/results")
    parser.add_argument("claim_id", help="the record to trace, e.g. C-87")
    parser.add_argument(
        "--kb", type=Path, default=None, help="the knowledge base; by default the one the run recorded in context.json"
    )
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    args = parser.parse_args(argv)
    try:
        data = trace(args.results_dir, args.claim_id, args.kb)
    except TraceInput as err:
        print(f"claim_trace: {err}", file=sys.stderr)
        return 2 if "no such directory" in str(err) or "no such knowledge base" in str(err) else 1
    print(json.dumps(data, indent=2, default=str) if args.format == "json" else render(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
