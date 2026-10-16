"""Diagnose a novelty stage from its archive, without reading it by hand.

    uv run python -m evaluation.novelty_diagnosis experiments/<RUN>/results [--format json|markdown]

`python -m pipeline.cli novelty` writes the same diagnosis (`13-novelty-diagnosis.json` and `.md`) when it ends, so
a stage that went wrong says so in its own archive.

What it reads, and what each source is good for:

- `13-novelty-report.json` — what the host accepted and refused, entry by entry, with the reason, and its
  `provenance` block: the works it kept, the works it dropped because the search tool never returned them, the
  works named in the rationale alone, and whether the hypothesis's own agent read each kept work in full or
  only its abstract. A report
  written before the host recorded those decisions carries no block, and the counts are then absent, not zero;
- `literature-calls.agents.jsonl` — the **search server's** own audit line per call (query, outcome, whether the
  cache served it). Not attributed to an agent: the server does not know who is asking;
- `13-novelty-agent-tools.jsonl` — the **workflow's** tool log, attributed to an agent by `label`
  (`novelty@H-n`), with the query truncated to 200 bytes;
- the search cache — which works each query actually returned, to check what an agent cites against what *this*
  stage's searches served (the host only checks against the whole cache).

Every finding carries a `code`, a `severity` (`problem` changes how the result should be read, `note` is context)
and the figures it rests on. The module states no cause: it says what the archive shows. A figure it cannot read is
reported absent, never as zero.

Exit status: 0 when no finding is a `problem`, 1 when one is, 2 on an input error.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from tools.literature.search import DEFAULT_CACHE, CacheMiss, search_works

REPORT = "13-novelty-report.json"
TOOL_LOG = "13-novelty-agent-tools.jsonl"
SERVER_LOG = "literature-calls.agents.jsonl"
SEARCH_TOOL = "mcp__literature__search_works"
OUT_JSON, OUT_MD = "13-novelty-diagnosis.json", "13-novelty-diagnosis.md"
THIN_ABSTRACT_SHARE = 0.5


class DiagnosisInput(ValueError):
    """The archive has no novelty stage to diagnose."""


def _rows(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text().splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _norm(query: str) -> str:
    return " ".join(query.lower().split())


def _query_of(preview: Any) -> str | None:
    """The query in a tool-log row's `input_preview`; None when the preview was cut before the value ended."""
    try:
        value = json.loads(preview)["query"]
    except (ValueError, KeyError, TypeError):
        return None
    return value if isinstance(value, str) else None


def _finding(code: str, severity: str, detail: str, **figures: Any) -> dict[str, Any]:
    return {"code": code, "severity": severity, "detail": detail, **figures}


def diagnose(results_dir: Path, cache_path: Path = DEFAULT_CACHE) -> dict[str, Any]:
    report_path = results_dir / REPORT
    if not report_path.is_file():
        raise DiagnosisInput(f"{report_path} does not exist: the novelty stage has not run on this archive")
    report = json.loads(report_path.read_text())
    findings: list[dict[str, Any]] = []
    entries = report.get("entries") or []
    launch = report.get("launch") or {}

    if report.get("status") == "not_assessed":
        findings.append(_finding("NO_HYPOTHESIS", "note", report.get("reason", "no live hypothesis")))
        return _result(results_dir, report, findings, [], {})

    # Only the novelty stage is granted `search_works`, so every `search` line in the shared log is this stage's.
    # A second attempt appends to the same log: the report records where this attempt's lines begin.
    own = _rows(results_dir / SERVER_LOG)[int(report.get("server_log_offset") or 0) :]
    searches = [r for r in own if r.get("event") == "search"]
    by_outcome: dict[str, int] = {}
    for row in searches:
        by_outcome[str(row.get("outcome"))] = by_outcome.get(str(row.get("outcome")), 0) + 1
    network = sum(1 for r in searches if r.get("from_cache") is False)
    served: dict[str, list[dict[str, Any]]] = {}
    for row in searches:
        if row.get("outcome") == "searched" and isinstance(row.get("query"), str):
            try:
                served[_norm(row["query"])] = search_works(row["query"], cache_path=cache_path, allow_network=False)[
                    "works"
                ]
            except (CacheMiss, ValueError, OSError):
                pass
    returned = [w for works in served.values() for w in works]

    tool_log_present = (results_dir / TOOL_LOG).is_file()
    tool_rows = _rows(results_dir / TOOL_LOG)
    per_agent: dict[str, dict[str, Any]] = {}
    for e in entries:
        label = f"novelty@{e.get('hypothesis_id')}"
        calls = [r for r in tool_rows if r.get("label") == label and r.get("tool") == SEARCH_TOOL]
        per_agent[e.get("hypothesis_id")] = {
            "search_calls": len(calls) if tool_log_present else None,
            "statuses": sorted({str(r.get("status")) for r in calls}),
            "queries_seen": [q for q in (_query_of(r.get("input_preview")) for r in calls) if q],
        }

    if not searches:
        findings.append(
            _finding(
                "NO_SEARCH",
                "problem",
                "the search server logged no search for this stage, so no " "verdict rests on a search",
                searches=0,
            )
        )
    for hid, info in per_agent.items():
        if info["search_calls"] == 0:
            findings.append(
                _finding(
                    "AGENT_DID_NOT_SEARCH",
                    "problem",
                    f"{hid}: the tool log shows no search_works call by its agent",
                    hypothesis_id=hid,
                )
            )
    for outcome in ("failed", "budget_spent", "bad_input"):
        if by_outcome.get(outcome):
            findings.append(
                _finding(
                    f"SEARCH_{outcome.upper()}",
                    "problem" if outcome != "bad_input" else "note",
                    f"{by_outcome[outcome]} search call(s) ended `{outcome}`",
                    count=by_outcome[outcome],
                )
            )

    logged = {_norm(r["query"]) for r in searches if isinstance(r.get("query"), str)}
    answered = {
        _norm(r["query"]) for r in searches if r.get("outcome") == "searched" and isinstance(r.get("query"), str)
    }
    for e in entries:
        stated = [_norm(q) for q in ((e.get("novelty") or {}).get("queries") or [])]
        missing = [q for q in stated if q not in logged]
        if missing:
            findings.append(
                _finding(
                    "QUERIES_NOT_IN_LOG",
                    "problem",
                    f"{e['hypothesis_id']}: the agent reports queries the server never logged",
                    hypothesis_id=e["hypothesis_id"],
                    reported=len(stated),
                    missing=missing,
                )
            )
        if (
            e.get("status") == "assessed"
            and (e.get("novelty") or {}).get("verdict") == "no_prior_art_found"
            and not any(q in answered for q in stated)
        ):
            findings.append(
                _finding(
                    "NO_PRIOR_ART_WITHOUT_A_SEARCH",
                    "problem",
                    f"{e['hypothesis_id']}: `no_prior_art_found` while none of its queries was "
                    "answered by the search tool, so no abstract was read",
                    hypothesis_id=e["hypothesis_id"],
                )
            )
        # The host's per-work decisions. Read before the `continue` below, so a refused entry still reports
        # what was dropped: the one probe that opened a full text was refused, and the reading was invisible.
        prov = e.get("provenance") or {}
        if prov.get("dropped"):
            findings.append(
                _finding(
                    "WORK_NOT_SERVED",
                    "problem",
                    f"{e['hypothesis_id']}: the host dropped {len(prov['dropped'])} work(s) the "
                    "search tool never returned",
                    hypothesis_id=e["hypothesis_id"],
                    works=[w.get("openalex_id") for w in prov["dropped"]],
                )
            )
        if prov.get("rationale_only"):
            findings.append(
                _finding(
                    "RATIONALE_WORK_UNLISTED",
                    "note",
                    f"{e['hypothesis_id']}: the rationale names work(s) the search tool did serve "
                    "but the agent left out of `closest_works`",
                    hypothesis_id=e["hypothesis_id"],
                    works=prov["rationale_only"],
                )
            )
        if e.get("status") != "assessed" and prov.get("works_listed") == 0 and prov.get("works_dropped"):
            findings.append(
                _finding(
                    "VERDICT_ON_NO_SERVED_WORK",
                    "problem",
                    f"{e['hypothesis_id']}: a verdict of prior art was left with no work the "
                    "search tool ever returned, so the host kept no verdict",
                    hypothesis_id=e["hypothesis_id"],
                    dropped=prov["works_dropped"],
                )
            )
        if e.get("status") != "assessed":
            findings.append(
                _finding(
                    "ENTRY_UNVERIFIED",
                    "problem",
                    f"{e['hypothesis_id']}: {e.get('error')}",
                    hypothesis_id=e["hypothesis_id"],
                )
            )
            continue
        ids_served = {w.get("openalex_id") for w in returned}
        outside = [
            w["openalex_id"]
            for w in (e.get("novelty") or {}).get("closest_works", [])
            if served and w["openalex_id"] not in ids_served
        ]
        if outside:
            findings.append(
                _finding(
                    "CITED_OUTSIDE_THIS_STAGE",
                    "problem",
                    f"{e['hypothesis_id']}: cites works that none of this stage's logged searches "
                    "returned (they are in the cache from an earlier stage)",
                    hypothesis_id=e["hypothesis_id"],
                    works=outside,
                )
            )
    if returned and sum(1 for w in returned if not w.get("abstract")) / len(returned) > THIN_ABSTRACT_SHARE:
        findings.append(
            _finding(
                "THIN_ABSTRACTS",
                "note",
                "more than half of the returned works carry no abstract, " "so a verdict rests on titles for them",
                without_abstract=sum(1 for w in returned if not w.get("abstract")),
                returned=len(returned),
            )
        )
    if report.get("dropped_agents"):
        findings.append(
            _finding("AGENT_DROPPED", "problem", "an agent returned nothing", agents=report["dropped_agents"])
        )
    reads = [r for r in own if r.get("event") == "read"]
    read_outcomes: dict[str, int] = {}
    for row in reads:
        read_outcomes[str(row.get("outcome"))] = read_outcomes.get(str(row.get("outcome")), 0) + 1
    fetches = [r for r in own if r.get("event") == "fetch" and r.get("outcome")]
    if read_outcomes.get("budget_spent"):
        findings.append(
            _finding(
                "READ_BUDGET_SPENT",
                "note",
                f"{read_outcomes['budget_spent']} read call(s) were " "refused for budget",
                count=read_outcomes["budget_spent"],
            )
        )
    paywalled = sum(1 for r in fetches if r.get("outcome") == "paywalled")
    if paywalled:
        findings.append(
            _finding(
                "PAYWALLED_WHEN_FETCHED",
                "note",
                f"{paywalled} paper(s) the agents tried to open " "were paywalled, so they stayed on abstracts",
                count=paywalled,
            )
        )
    # A report written before the host recorded its per-work decisions carries no block: the counts are then
    # unknown, not zero.
    blocks = [e["provenance"] for e in entries if isinstance(e.get("provenance"), dict)]
    counted = {
        key: (sum(b.get(key) or 0 for b in blocks) if blocks else None)
        for key in ("works_listed", "works_dropped", "works_read_in_full")
    }
    # Two hypotheses may cite the same full text, so `works_read_in_full` (a sum of per-entry counts) counts it
    # twice: this is the union of the works marked `full_text`, the one of the two that is comparable with
    # `papers_opened` below. Refused entries count here too, like the three sums above.
    read_in_full = {
        w.get("openalex_id") for b in blocks for w in (b.get("kept") or []) if w.get("evidence_depth") == "full_text"
    }
    figures = {
        **counted,
        "distinct_works_read_in_full": len(read_in_full) if blocks else None,
        "read_outcomes": read_outcomes,
        "papers_opened": len({r.get("paper") for r in reads if r.get("outcome") == "read"}),
        "fetch_outcomes": {
            o: sum(1 for r in fetches if r.get("outcome") == o)
            for o in sorted({str(r.get("outcome")) for r in fetches})
        },
        "searches_logged": len(searches),
        "searches_on_the_network": network,
        "search_outcomes": by_outcome,
        "works_returned": len(returned),
        "distinct_works_returned": len({w.get("openalex_id") for w in returned}),
        "cost_usd": launch.get("session_cost_usd"),
        "wall_s": launch.get("wall_s"),
    }
    return _result(results_dir, report, findings, entries, per_agent, figures)


def _result(
    results_dir: Path,
    report: dict[str, Any],
    findings: list[dict[str, Any]],
    entries: list,
    per_agent: dict[str, Any],
    figures: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "results_dir": str(results_dir),
        "stage_status": report.get("status"),
        "entries": [
            {
                "hypothesis_id": e.get("hypothesis_id"),
                "status": e.get("status"),
                "verdict": (e.get("novelty") or {}).get("verdict"),
                **{
                    key: (e.get("provenance") or {}).get(key) if isinstance(e.get("provenance"), dict) else None
                    for key in ("works_listed", "works_dropped", "works_read_in_full")
                },
                **per_agent.get(e.get("hypothesis_id"), {}),
            }
            for e in entries
        ],
        "figures": figures or {},
        "findings": findings,
        "problems": sum(1 for f in findings if f["severity"] == "problem"),
    }


def render(data: dict[str, Any]) -> str:
    lines = [
        f"# Novelty stage diagnosis — {data['results_dir']}",
        "",
        f"Stage status: `{data['stage_status']}`. {data['problems']} problem(s), "
        f"{len(data['findings']) - data['problems']} note(s). This states what the archive shows, not why.",
        "",
    ]
    if data["figures"]:
        lines += ["## Figures", ""] + [f"- {k}: `{json.dumps(v)}`" for k, v in data["figures"].items()] + [""]
    if data["entries"]:
        lines += [
            "## Per hypothesis",
            "",
            "| hypothesis | status | verdict | works kept/dropped/read in full | search calls | statuses |",
            "|---|---|---|---|---|---|",
        ]
        for e in data["entries"]:
            calls = "absent" if e.get("search_calls") is None else e["search_calls"]
            works = (
                "absent"
                if e.get("works_listed") is None
                else f"{e['works_listed']}/{e.get('works_dropped')}/{e.get('works_read_in_full')}"
            )
            lines.append(
                f"| {e['hypothesis_id']} | {e['status']} | {e.get('verdict') or '-'} | {works} | "
                f"{calls} | {', '.join(e.get('statuses') or []) or '-'} |"
            )
        lines.append("")
    lines += ["## Findings", ""]
    lines += [f"- **{f['severity']}** `{f['code']}` — {f['detail']}" for f in data["findings"]] or ["- none"]
    return "\n".join(lines) + "\n"


def write(results_dir: Path, cache_path: Path = DEFAULT_CACHE) -> dict[str, Any]:
    data = diagnose(results_dir, cache_path)
    (results_dir / OUT_JSON).write_text(json.dumps(data, indent=2) + "\n")
    (results_dir / OUT_MD).write_text(render(data))
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("results_dir", type=Path)
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    args = parser.parse_args(argv)
    try:
        data = diagnose(args.results_dir, args.cache)
    except DiagnosisInput as err:
        print(f"novelty_diagnosis: {err}", file=sys.stderr)
        return 2
    print(json.dumps(data, indent=2) if args.format == "json" else render(data))
    return 1 if data["problems"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
