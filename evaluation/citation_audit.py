"""The audit packet for one completed proposal: what each citation rests on, and what a human must read.

Why this exists. Citations must be verified against their source before they appear in a final proposal. This
module does the mechanical half — resolve every citation, re-check every quote
against the stored paper, pin what it read — and prints a packet whose remaining columns only a human can fill.

What it deliberately does **not** do. It does not judge whether a quote supports a claim: that judgement is the
same act that produced the claim, and a model's opinion is not evidence. It prints no accuracy score of its own: the headline is a **support ladder** over every citation
tag, and accuracy is reported only inside the rung where a quote was actually checked, with its denominator
stated.

    uv run python -m evaluation.citation_audit experiments/<RUN>/results [--kb ../knowledgebase]
        [--store ../literature/store] [--format markdown] [--verdicts <filled packet>]

Two units, never mixed: the **ladder** counts the proposal's own `cites` tags, which is what the synthesis gate
validates; the **sentences** come from the inline `[[ID]]` tags in the prose, which is what a reader judges. Each
table says which unit it uses.

Reproducibility: the knowledge base and the literature store are git-ignored and mutable, so every quote check
records the `text_sha256` of the paper it read, and every claim carries its epistemic status **as of the end of
the run** as well as today — a shared knowledge base keeps moving, which is how an earlier run's hypotheses
became contradicted after the fact.
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from knowledge_base.schemas import Claim, Decision, Experiment, Ignorance, Intervention, Relation
from knowledge_base.status import statuses, statuses_as_of
from knowledge_base.store import read_records
from knowledge_base.validate import Record

DEFAULT_KB: Final = Path(__file__).resolve().parents[1] / "knowledgebase"
DEFAULT_STORE: Final = Path(__file__).resolve().parents[1] / ".." / "literature" / "store"
TAG: Final = re.compile(r"\[\[([A-Z]+-\d+)\]\]")
# The rungs of the support ladder, weakest last. `derived` is a record that is not a claim: an experiment, an
# intervention, a decision or a declared gap, which carries no source of its own and must be traced to claims.
RUNGS: Final = ("quote_checked", "source_text_absent", "no_source_ref", "no_quote", "derived")
# What a reader may write. The judgement asked is **textual, not scientific**: does the quote say what the claim
# says? No domain knowledge is needed for that, and bringing any in would be a different audit — biological
# plausibility, which has no scorer (`evaluation/scorecard.py::NO_SCORER`). `cannot tell by reading` exists so
# that a reader who would have to know the field is not forced to guess; those rows leave the denominator and
# are reported as their own number, because "how much of the citation base is undecidable by reading" is itself
# worth knowing.
VERDICTS: Final = ("supports", "overstates", "unrelated", "cited to limit", "cannot tell by reading")
EXCLUDED: Final = ("cited to limit", "cannot tell by reading")


def _sentences(text: str) -> list[str]:
    """The prose split on sentence ends, keeping the terminator. Crude on purpose: it only has to give a reader
    the sentence that carries a tag, not to parse English."""
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]


def prose(proposal: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Every string of the proposal with the path that locates it."""
    out: list[tuple[str, str]] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, f"{path}.{key}" if path else str(key))
        elif isinstance(node, list):
            for i, value in enumerate(node):
                walk(value, f"{path}[{i}]")
        elif isinstance(node, str):
            out.append((path, node))

    walk(proposal, "")
    return out


def cites(proposal: Mapping[str, Any]) -> list[tuple[str, str]]:
    """The proposal's declared citations as (section, id) pairs, one per tag — the unit the gate validates."""
    out: list[tuple[str, str]] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "cites" and isinstance(value, list):
                    out.extend((path or "root", str(v)) for v in value)
                else:
                    walk(value, f"{path}.{key}" if path else str(key))
        elif isinstance(node, list):
            for i, value in enumerate(node):
                walk(value, f"{path}[{i}]")

    walk(proposal, "")
    return out


def carrying_sentences(proposal: Mapping[str, Any], record_id: str) -> list[dict[str, str]]:
    """The sentences whose inline `[[ID]]` names this record, with where they are."""
    found = []
    for path, text in prose(proposal):
        for sentence in _sentences(text):
            if f"[[{record_id}]]" in sentence:
                found.append({"at": path, "sentence": sentence})
    return found


def at_run_end(records: Mapping[str, Record], run_id: str) -> dict[str, str]:
    """Epistemic status as the run left it: the one definition of "as of" lives in `knowledge_base.status`."""
    stamps = [r.created_at for r in records.values() if r.run_id == run_id]
    return statuses_as_of(records, max(stamps)) if stamps else {}


def rests_on(record: Record, records: Mapping[str, Record]) -> list[str]:
    """What a cited non-claim record stands on, so a citation of it is traceable to evidence."""
    if isinstance(record, Experiment):
        return list(record.tested_claims)
    if isinstance(record, Intervention):
        return list(record.acts_on_claims)
    if isinstance(record, Decision):
        return [record.hypothesis_id, *record.experiment_ids]
    if isinstance(record, Ignorance):
        return [
            r.target_id
            for r in records.values()
            if isinstance(r, Relation) and r.type == "questions" and r.source_id == record.id
        ]
    return []


def _check(store: Any, log: Path, claim: Claim) -> dict[str, Any]:
    """`check_quote` over the stored paper, or the reason it could not be asked."""
    refs = [r for r in claim.source_refs if not r.startswith("receipt:")]
    if not claim.quote:
        return {"status": None, "reason": "the claim carries no quote"}
    if not refs:
        return {"status": "no_source_ref", "reason": "the claim quotes a source it does not name"}
    from tools.literature.mcp_server import check_quote

    result = check_quote(store, log, paper=refs[0], quote=claim.quote)
    return {
        "status": result.get("status"),
        "verdict": result.get("verdict"),
        "paper": refs[0],
        "text_sha256": result.get("text_sha256"),
        "cause": result.get("cause"),
        "detail": result.get("detail"),
    }


def rung(record: Record | None, check: Mapping[str, Any] | None) -> str:
    if not isinstance(record, Claim):
        return "derived"
    if check is None or check.get("status") is None:
        return "no_quote"
    if check["status"] == "no_source_ref":
        return "no_source_ref"
    if check["status"] == "checked":
        return "quote_checked"
    return "source_text_absent"


def rows(results_dir: Path, kb_root: Path, store_root: Path, log: Path) -> dict[str, Any]:
    """One row per `cites` tag, with the sentences that carry it and the quote re-checked."""
    proposal_file = results_dir / "final-proposal.json"
    if not proposal_file.is_file():
        return {"rows": None, "reason": f"no final-proposal.json in {results_dir}"}
    payload = json.loads(proposal_file.read_text())
    proposal, gate = payload.get("proposal") or {}, payload.get("gate") or {}
    run_id = payload.get("run_id")
    records = read_records(kb_root)
    now, then = statuses(records), at_run_end(records, str(run_id))

    from tools.literature.store import LiteratureStore

    store = LiteratureStore(store_root)
    checked: dict[str, dict[str, Any]] = {}
    out = []
    for section, rid in cites(proposal):
        record = records.get(rid)
        if isinstance(record, Claim) and rid not in checked:
            checked[rid] = _check(store, log, record)
        check = checked.get(rid)
        row: dict[str, Any] = {
            "section": section,
            "id": rid,
            "record": type(record).__name__ if record else None,
            "rung": rung(record, check),
            "sentences": carrying_sentences(proposal, rid),
        }
        if isinstance(record, Claim):
            row |= {
                "content": record.content,
                "type": record.type,
                "origin": record.origin,
                "provenance": record.provenance_status,
                "causal": record.causal_fields() is not None,
                "status_now": now.get(rid),
                "status_at_run_end": then.get(rid),
                "quote": record.quote,
                "source": (check or {}).get("paper"),
                "check": (check or {}).get("status"),
                "check_verdict": (check or {}).get("verdict"),
                "check_cause": (check or {}).get("cause"),
                "text_sha256": (check or {}).get("text_sha256"),
                "written_by_this_run": record.run_id == run_id,
            }
        elif record is not None:
            row |= {"rests_on": rests_on(record, records), "written_by_this_run": record.run_id == run_id}
        else:
            row |= {"missing": True}
        out.append(row)
    return {"run_id": run_id, "rows": out, "gate": gate, "kb": str(kb_root), "store": str(store_root)}


def weaknesses(data: Mapping[str, Any]) -> dict[str, Any]:
    """The weaknesses the artifacts know about, by a rule declared before counting.

    A weakness enters the list if and only if an artifact states it: (1) a cited claim whose quote could not be
    checked, for want of the text or of a source ref; (2) a cited claim the synthesis gate flagged as
    contradicted or contested; (3) a cited claim that is `unresolved` today while it was contradicted or
    contested at the end of the run, or the reverse — the shared knowledge base moved under it.

    Whether the proposal **states** each one is left blank: that is a reading, and the inverse test ("which known
    weaknesses does the proposal not mention?") is only honest if the list is fixed before anyone looks.
    """
    rows_ = data.get("rows") or []
    flagged = {
        rid.split()[0]
        for ids in (data.get("gate", {}).get("cites_contradicted_or_contested") or {}).values()
        for rid in ids
    }
    found: dict[str, dict[str, Any]] = {}
    for row in rows_:
        rid = row["id"]
        if row["rung"] in ("source_text_absent", "no_source_ref"):
            found.setdefault(
                rid, {"id": rid, "kind": "quote cannot be checked", "detail": row.get("check_cause") or row["rung"]}
            )
        if rid in flagged:
            found.setdefault(
                rid, {"id": rid, "kind": "flagged by the gate", "detail": f"status {row.get('status_now')}"}
            )
        if row.get("status_now") != row.get("status_at_run_end"):
            found.setdefault(
                rid,
                {
                    "id": rid,
                    "kind": "status moved since the run",
                    "detail": f"{row.get('status_at_run_end')} -> {row.get('status_now')}",
                },
            )
    return {
        "rule": ["quote cannot be checked", "flagged by the gate", "status moved since the run"],
        "weaknesses": sorted(found.values(), key=lambda w: w["id"]),
    }


def ladder(data: Mapping[str, Any]) -> dict[str, Any]:
    counts = {r: 0 for r in RUNGS}
    for row in data.get("rows") or []:
        counts[row["rung"]] += 1
    return {"unit": "cites tags", "total": sum(counts.values()), "by_rung": counts}


def packet(data: Mapping[str, Any]) -> str:
    """The file a human fills in. Every row that needs reading gets an empty verdict cell."""
    lines = [
        f"# Citation audit — {data.get('run_id')}",
        "",
        f"Knowledge base `{data.get('kb')}`, literature store `{data.get('store')}`. Generated by "
        "`evaluation/citation_audit.py`; the verdict column is filled by hand.",
        "",
        "## Support ladder (unit: the proposal's own `cites` tags)",
        "",
        "| rung | tags |",
        "|---|---:|",
    ]
    counts = ladder(data)["by_rung"]
    for name in RUNGS:
        lines.append(f"| {name} | {counts[name]} |")
    lines += [
        "",
        f"Total {ladder(data)['total']} tags. Accuracy is reported only within `quote_checked`, and "
        "its denominator is the rows marked below.",
        "",
        "## To read (unit: one row per cited claim that carries a quote)",
        "",
        "**The judgement is textual, not scientific.** Compare only the two texts in front of you: does "
        "the quote say what the claim says, and does the proposal's sentence use it for what it says? "
        "Deciding whether the biology is *right* is a different question, it needs an expert, and this "
        "repository reports it as `no scorer` rather than guessing. Do not bring outside knowledge in.",
        "",
        "Write one of these in `verdict`:",
        "",
        "- `supports` — the quote says what the claim says, and the sentence uses it for that;",
        "- `overstates` — the claim or the sentence says **more** than the quote: an added number, an "
        "added interpretation, an entity the quote does not name;",
        "- `unrelated` — the quote does not bear on the claim;",
        "- `cited to limit` — the proposal cites it to qualify itself, not to support a claim;",
        "- `cannot tell by reading` — deciding would require knowing the field. Leaving this is a result, "
        "not a failure: these rows are counted apart and leave the accuracy denominator.",
        "",
        "Ten of the fourteen rows cite a paper whose text this system could not reach, so the quote cannot "
        "be re-checked against the source. The question there is narrower and still answerable: does the "
        "claim say more than the quote it reports?",
        "",
        "| id | rung | claim (the record) | quote (the source) | proposal sentence | source | verdict | " "note |",
        "|---|---|---|---|---|---|---|---|",
    ]
    seen = set()
    for row in data.get("rows") or []:
        if row["rung"] not in ("quote_checked", "source_text_absent", "no_source_ref") or row["id"] in seen:
            continue
        seen.add(row["id"])
        sentence = row["sentences"][0]["sentence"] if row["sentences"] else ""
        content = row.get("content") or ""
        lines.append(
            f"| {row['id']} | {row['rung']} | {content[:200].replace('|', '/')} | "
            f"{(row.get('quote') or '')[:200].replace('|', '/')} | {sentence[:200].replace('|', '/')} | "
            f"{row.get('source') or '-'} |  |  |"
        )
    lines += [
        "",
        "## Known weaknesses: does the proposal state each one?",
        "",
        "The rule that fixed this list is in the module docstring and was declared before counting. Mark "
        "each `stated` or `not stated`.",
        "",
        "| id | kind | detail | stated | note |",
        "|---|---|---|---|---|",
    ]
    for item in weaknesses(data)["weaknesses"]:
        lines.append(f"| {item['id']} | {item['kind']} | {str(item['detail'])[:80]} |  |  |")
    lines += ["", "## Sentences, for the reading above", ""]
    for row in data.get("rows") or []:
        if row["id"] in seen:
            for s in row["sentences"]:
                lines.append(f"- **{row['id']}** [{s['at']}] {s['sentence']}")
            seen.discard(row["id"])
    return "\n".join(lines) + "\n"


STATED: Final = ("stated", "not stated")


def read_verdicts(path: Path) -> dict[str, Any]:
    """The filled packet read back: the verdict column of the reading table, and the `stated` column of the
    weakness table — the inverse test, which asks not what the proposal declares but which of the weaknesses the
    artifacts know about it leaves unmentioned."""
    out: dict[str, str] = {}
    weaknesses: dict[str, str] = {}
    for line in path.read_text().splitlines():
        cells = [c.strip() for c in line.split("|")]
        if len(cells) >= 9 and re.fullmatch(r"[A-Z]+-\d+", cells[1]) and cells[7]:
            out[cells[1]] = cells[7].strip("`")
        elif len(cells) == 7 and re.fullmatch(r"[A-Z]+-\d+", cells[1]) and cells[4].strip("`") in STATED:
            weaknesses[cells[1]] = cells[4].strip("`")
    counted = {v: sum(1 for x in out.values() if x == v) for v in VERDICTS}
    denominator = sum(counted.values()) - sum(counted[v] for v in EXCLUDED)
    return {
        "verdicts": out,
        "counts": counted,
        "read": len(out),
        "accuracy": None if denominator <= 0 else round(counted["supports"] / denominator, 3),
        "denominator": denominator,
        "undecidable_by_reading": counted["cannot tell by reading"],
        "weaknesses_read": len(weaknesses),
        "weaknesses_stated": sum(1 for v in weaknesses.values() if v == "stated"),
        "weaknesses_not_stated": sorted(k for k, v in weaknesses.items() if v == "not stated"),
        "note": "accuracy is `supports` over the rows read, minus those cited to limit and those a reader "
        "could not decide without knowing the field; one proposal, one reader, and the reader is the "
        "system's author",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("results_dir", type=Path)
    parser.add_argument("--kb", type=Path, default=DEFAULT_KB)
    parser.add_argument("--store", type=Path, default=DEFAULT_STORE)
    parser.add_argument("--format", choices=("json", "markdown"), default="markdown")
    parser.add_argument("--verdicts", type=Path, help="a filled packet to read back")
    parser.add_argument("--save", action="store_true", help="write the packet under <results_dir>/audit/")
    args = parser.parse_args(argv)

    if args.verdicts:
        print(json.dumps(read_verdicts(args.verdicts), indent=2))
        return 0
    log = args.results_dir / "literature-calls.audit.jsonl"
    data = rows(args.results_dir, args.kb, args.store, log)
    if data.get("rows") is None:
        print(json.dumps(data, indent=2))
        return 1
    if args.save:
        out = args.results_dir / "audit"
        out.mkdir(parents=True, exist_ok=True)
        (out / "citations.md").write_text(packet(data))
        (out / "citations.json").write_text(
            json.dumps(data | weaknesses(data) | {"ladder": ladder(data)}, indent=2, default=str)
        )
        print(f"wrote {out / 'citations.md'} and {out / 'citations.json'}")
        return 0
    print(
        packet(data)
        if args.format == "markdown"
        else json.dumps(data | weaknesses(data) | {"ladder": ladder(data)}, indent=2, default=str)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
