"""The message exchanges of a run.sh run, for the Story tab.

Two records exist and only one is timestamped:

- `prompts.jsonl`: one line per agent spawn — when, who sent it (`produced_by`), who received it
  (`to_whom`, `label`, `agent_type`), and the prompt. Timestamped.
- the workflow journals (`ca-journal.jsonl`, `discovery-journal.jsonl`): a `result` line per agent,
  carrying the reply but no timestamp, tied to its spawn by `agentId`.

A run of the phase pipeline has neither `prompts.jsonl` nor those journals: each launched phase writes
`NN-<phase>-journal.jsonl` with the same `started`/`result` lines, so its exchanges come in phase order, and its
narrative is one line per phase report. Each exchange also carries what the archive holds of the agent's own
activity: its tool calls in order (`NN-<phase>-agent-tools.jsonl`, truncated previews) and, when the run archived
the agents' transcripts (`transcripts/<stage>/`), the prompt it received and when. Without a transcript the prompt
and time stay empty; without an audit trail `tools` is None, not an empty list.

In the phase pipeline too, agents never address each other: a phase workflow computes one prompt per agent
(`agent(prompt(item), ...)` in `workflows/phases/src/*.js`) and keeps its structured result; what one agent
learns from another reaches it only through the knowledge base, as records packed into its prompt or read with
the `mcp__kb__*` tools, which appear among its tool calls.

Agents never message each other in this workflow: the workflow script sends each one a prompt and
receives a reply. So an exchange is `workflow -> agent` plus that agent's reply, and the reply's time
is reported as unknown (`None`) rather than guessed.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .phase_reports import phase_usages
from .pipeline_run import agent_prompt, stage_tool_calls

JOURNALS = ("ca-journal.jsonl", "discovery-journal.jsonl", "journal.jsonl")
PHASE_JOURNAL_GLOB = "[0-9][0-9]-*-journal.jsonl"
TEXT_LIMIT = 6000


def _jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue  # a live run is appended to; a half-written last line is normal
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _clip(value: Any) -> tuple[str | None, bool]:
    if value is None:
        return None, False
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=1)
    return (text[:TEXT_LIMIT], True) if len(text) > TEXT_LIMIT else (text, False)


def _journal_files(results_dir: Path) -> list[tuple[Path, str | None]]:
    """The journals to read with the pipeline stage each belongs to (None for the run.sh journals)."""
    files: list[tuple[Path, str | None]] = [(results_dir / name, None) for name in JOURNALS]
    files += [(p, p.name.removesuffix("-journal.jsonl")) for p in sorted(results_dir.glob(PHASE_JOURNAL_GLOB))]
    return files


def _journal_replies(
    results_dir: Path,
) -> tuple[dict[str, Any], dict[str, tuple[str | None, str | None]], dict[str, str]]:
    """Unclipped replies, (label, phase) and pipeline stage by agentId, across the per-stage journals."""
    replies: dict[str, Any] = {}
    labels: dict[str, tuple[str | None, str | None]] = {}
    stages: dict[str, str] = {}
    seen_journals: set[str] = set()
    for path, stage in _journal_files(results_dir):
        rows = _jsonl(path)
        # journal.jsonl duplicates the per-stage journals; count each agent once.
        for row in rows:
            agent_id = row.get("agentId")
            if not agent_id or agent_id in seen_journals and row.get("type") != "result":
                continue
            if row.get("type") == "started":
                labels[agent_id] = (row.get("label"), row.get("phase"))
                if stage:
                    stages[agent_id] = stage
            elif row.get("type") == "result":
                replies[agent_id] = row.get("result")
        seen_journals.update(a for a in labels)
    return replies, labels, stages


def _verdict(reply: Any) -> str | None:
    """The critic's verdict, read from the unclipped reply (the clipped text may not parse)."""
    verdict = reply.get("verdict") if isinstance(reply, dict) else None
    return verdict if isinstance(verdict, str) else None


def run_exchanges(results_dir: Path) -> list[dict[str, Any]]:
    """Exchanges ordered by prompt time; agents with no prompt record follow, with time None."""
    replies, labels, stages = _journal_replies(results_dir)
    exchanges: list[dict[str, Any]] = []
    prompted: set[str] = set()
    for row in _jsonl(results_dir / "prompts.jsonl"):
        agent_id = str(row.get("agent_id") or "")
        prompted.add(agent_id)
        prompt, prompt_cut = _clip(row.get("prompt"))
        reply, reply_cut = _clip(replies.get(agent_id))
        exchanges.append(
            {
                "timestamp": row.get("timestamp"),
                "stage": row.get("stage"),
                "phase": row.get("phase"),
                "from": row.get("produced_by"),
                "to": row.get("label") or row.get("to_whom"),
                "agent_type": row.get("agent_type"),
                "agent_id": agent_id,
                "attempt": row.get("attempt"),
                "prompt": prompt,
                "prompt_truncated": prompt_cut,
                "reply": reply,
                "reply_truncated": reply_cut,
                "verdict": _verdict(replies.get(agent_id)),
            }
        )
    exchanges.sort(key=lambda e: e["timestamp"] or "")

    trails: dict[str, dict[str, list[dict[str, Any]]] | None] = {}
    for agent_id, (label, phase) in labels.items():
        if agent_id in prompted:
            continue
        stage = stages.get(agent_id)
        reply, reply_cut = _clip(replies.get(agent_id))
        raw_prompt, prompt_time = agent_prompt(results_dir, stage, agent_id) if stage else (None, None)
        prompt, prompt_cut = _clip(raw_prompt)
        if stage and stage not in trails:
            trails[stage] = stage_tool_calls(results_dir, stage)
        trail = trails.get(stage) if stage else None
        exchanges.append(
            {
                "timestamp": prompt_time,
                "stage": stage,
                "phase": phase,
                "from": f"{phase or stage} workflow" if prompt else None,
                "to": label,
                "agent_type": None,
                "agent_id": agent_id,
                "attempt": None,
                "prompt": prompt,
                "prompt_truncated": prompt_cut,
                "reply": reply,
                "reply_truncated": reply_cut,
                "verdict": _verdict(replies.get(agent_id)),
                # None: this phase attempt has no tool-call audit trail; []: it has one and this agent called nothing.
                "tools": None if trail is None else [_tool_row(r) for r in trail.get(agent_id, [])],
            }
        )
    return exchanges


def _tool_row(row: dict[str, Any]) -> dict[str, Any]:
    """One tool call as the audit trail recorded it (input and result already truncated by the extractor)."""
    return {
        k: row.get(k) for k in ("ordinal", "tool", "at", "status", "input_preview", "result_preview", "result_bytes")
    }


def _by_prefix(
    replies: dict[str, Any], labels: dict[str, tuple[str | None, str | None]], phase: str
) -> list[dict[str, Any]]:
    return [r for a, r in replies.items() if labels.get(a, (None, None))[1] == phase and isinstance(r, dict)]


def narrate(results_dir: Path) -> list[str]:
    """A plain-language account of the run, one paragraph per step, built only from recorded data.

    Every sentence is a template filled from a journal reply; a step whose data is absent produces no
    sentence rather than a guessed one. No model writes this text.
    """
    replies, labels, stages = _journal_replies(results_dir)
    out: list[str] = _pipeline_narrative(results_dir, labels, replies, stages)
    status = _jsonl_one(results_dir / "discovery-result.json")

    evidence = _by_prefix(replies, labels, "Evidence")
    if evidence:
        n = sum(len(e.get("items", [])) for e in evidence)
        out.append(f"{len(evidence)} evidence agents searched independently and returned {n} evidence items.")

    checks = [c for r in _by_prefix(replies, labels, "EvidenceCheck") for c in r.get("checks", [])]
    if checks:
        counts: dict[str, int] = {}
        for c in checks:
            counts[str(c.get("status"))] = counts.get(str(c.get("status")), 0) + 1
        parts = ", ".join(f"{n} {k}" for k, n in sorted(counts.items(), key=lambda kv: -kv[1]))
        out.append(f"The evidence checker examined {len(checks)} records: {parts}.")

    analyses = _by_prefix(replies, labels, "ComputationalAnalysis")
    if analyses:
        ids = ", ".join(str(a.get("analysis_id")) for a in analyses)
        out.append(f"Computational analysis was recorded for: {ids}.")

    for g in _by_prefix(replies, labels, "CausalGraph"):
        causal = sum(1 for e in g.get("edges", []) if e.get("relation") == "causal")
        out.append(
            f"A prior causal graph was built with {len(g.get('nodes', []))} nodes and {len(g.get('edges', []))} edges "
            f"({causal} causal, the rest associational); {len(g.get('gaps', []))} evidence gaps were listed."
        )

    hyps = [h for r in _by_prefix(replies, labels, "Hypotheses") for h in r.get("hypotheses", [])]
    if hyps:
        out.append(
            f"The hypothesis generator proposed {len(hyps)} competing hypotheses: {', '.join(str(h.get('id')) for h in hyps)}."
        )

    critiques = sorted(_by_prefix(replies, labels, "Critique"), key=lambda c: str(c.get("hypothesis_id")))
    if critiques:
        verdicts = "; ".join(
            f"{c.get('hypothesis_id')} {c.get('verdict')} ({len(c.get('objections', []))} objections)"
            for c in critiques
        )
        out.append(f"The critic reviewed each hypothesis: {verdicts}.")

    for r in _by_prefix(replies, labels, "Revision"):
        out.append(f"{r.get('parent_id')} was revised in answer to critique round {r.get('answers_critique_round')}.")

    if status.get("status"):
        bits = [f"The workflow ended with status {status['status']}"]
        if status.get("cost_usd") is not None:
            bits.append(f"estimated cost ${float(status['cost_usd']):.2f}")
        ms = (status.get("usage") or {}).get("duration_ms")
        if ms:
            bits.append(f"duration {ms / 60000:.1f} min")
        out.append(", ".join(bits) + ".")
    return out


def _pipeline_narrative(
    results_dir: Path,
    labels: dict[str, tuple[str | None, str | None]],
    replies: dict[str, Any],
    stages: dict[str, str],
) -> list[str]:
    """One sentence per phase report of a phase-pipeline run: agents started and answered, then the launch figures.

    Every figure is read from the report or the phase journal; a figure the report lacks is left out.
    """
    out: list[str] = []
    for usage in phase_usages(results_dir):
        if not usage.launched:
            out.append(f"Phase {usage.phase}: no agent launch recorded.")
            continue
        started = [a for a, s in stages.items() if s == usage.launch_stage and a in labels]
        answered = sum(1 for a in started if a in replies)
        name = f"{usage.phase} (copied from the original run, not re-run here)" if usage.inherited else usage.phase
        bits = [f"Phase {name}: {len(started)} agents started, {answered} returned a result"]
        if usage.workflow_status:
            bits.append(f"workflow {usage.workflow_status}")
        if usage.cost_usd is not None:
            bits.append(f"CLI-reported cost ${usage.cost_usd:.2f}")
        if usage.total_tokens is not None:
            bits.append(f"{usage.total_tokens:,} tokens")
        if usage.wall_s is not None:
            bits.append(f"{usage.wall_s / 60:.1f} min")
        if usage.earlier_attempts:
            bits.append(f"attempt {usage.launch_stage} after {len(usage.earlier_attempts)} earlier attempt(s)")
        out.append("; ".join(bits) + ".")
    return out


def _jsonl_one(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


_COMPLETED = re.compile(r"^\[seq \d+\] Agent completed: (\S+)(?: \((.*)\))?$")
_SEQ = re.compile(r"^\[seq \d+\] ")
_DROPPED = re.compile(r"^(\S+): dropped (\S+): (.*)$")
_REVISION_REJECTED = re.compile(r"^Revision rejected: (\S+): (.*)$")


def _note(headline: str, tone: str, details: list[str]) -> dict[str, Any]:
    return {"headline": headline, "tone": tone, "details": details}


def workflow_notes(results_dir: Path) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    """Neutral events the workflow logged, keyed by the agent they follow, plus the closing events.

    The workflow's own log lines say what it did with an agent's reply (dropped a record, rejected a
    revision, kept N items). They are grouped and given a one-line headline by fixed patterns; the raw
    lines stay available as details, and an unrecognised line is shown verbatim, never reworded.
    """
    result = _jsonl_one(results_dir / "discovery-result.json")
    logs = result.get("logs") if isinstance(result.get("logs"), list) else []
    by_agent: dict[str, list[dict[str, Any]]] = {}
    closing: list[dict[str, Any]] = []
    last: str | None = None
    group: dict[str, Any] | None = None  # the open group of same-kind lines

    def add(note: dict[str, Any], anchor: str | None = None) -> None:
        anchor = anchor or last
        (by_agent.setdefault(anchor, []) if anchor else closing).append(note)

    for raw in map(str, logs):
        done = _COMPLETED.match(raw)
        if done:
            last, group = done.group(1), None
            if done.group(2):
                add(_note(f"{last} finished: {done.group(2)}", "info", []))
            continue
        if raw.startswith("[seq") and "Agent started" in raw:
            group = None
            continue
        line = _SEQ.sub("", raw)
        if m := _DROPPED.match(line):
            if group is None or group["kind"] != ("dropped", m.group(1)):
                group = {"kind": ("dropped", m.group(1)), "note": _note("", "warn", [])}
                # Logged while that agent is still running, i.e. before its own "completed" line.
                add(group["note"], anchor=m.group(1))
            group["note"]["details"].append(f"{m.group(2)}: {m.group(3)}")
            n = len(group["note"]["details"])
            group["note"]["headline"] = f"{m.group(1)}: {n} evidence record{'s' if n != 1 else ''} dropped"
        elif line.startswith("Critic evidence retained as unverified"):
            if group is None or group["kind"] != "unverified":
                group = {"kind": "unverified", "note": _note("", "warn", [])}
                add(group["note"])
            group["note"]["details"].append(line.split(": ", 1)[-1])
            n = len(group["note"]["details"])
            group["note"]["headline"] = f"{n} critic-cited source{'s' if n != 1 else ''} kept as unverified"
        elif m := _REVISION_REJECTED.match(line):
            group = None
            add(_note(f"Revision {m.group(1)} rejected", "bad", [m.group(2)]))
        else:
            group = None
            add(_note(line, "info", []))

    body = result.get("result") if isinstance(result.get("result"), dict) else {}
    if body.get("outcome"):
        details = [f"{k}: {v}" for k, v in (body.get("revision_summary") or {}).items()]
        details += [
            f"interventions: {len(body.get('interventions') or [])}",
            f"experiments: {len(body.get('experiments') or [])}",
        ]
        headline = f"Run ended: {str(body['outcome']).replace('_', ' ')}" + (
            f" — {body['reason']}" if body.get("reason") else ""
        )
        closing.append(_note(headline, "warn" if body["outcome"] == "insufficient_evidence" else "ok", details))
    elif result.get("status"):
        closing.append(
            _note(
                f"Workflow ended with status {result['status']}",
                "bad",
                [str(result["error"])] if result.get("error") else [],
            )
        )
    return by_agent, closing


def run_story(results_dir: Path) -> dict[str, Any]:
    exchanges = run_exchanges(results_dir)
    by_agent, closing = workflow_notes(results_dir)
    for label, notes in by_agent.items():
        target = next((e for e in reversed(exchanges) if e["to"] == label), None)
        if target is None:
            closing = notes + closing  # nothing to hang it on: keep it, do not drop it
        else:
            target["notes"] = notes
    kind = "phase_pipeline" if any(results_dir.glob(PHASE_JOURNAL_GLOB)) else "run_sh"
    return {"kind": kind, "narrative": narrate(results_dir), "exchanges": exchanges, "closing": closing}
