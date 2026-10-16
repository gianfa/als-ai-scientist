"""Would a hypothesis-revision phase placed after `verify` ever have had anything to work on?

Why this exists. A `refine` phase (revising a hypothesis after `verify`) is only worth building if its entry
condition fires. The condition chosen is a **verified anomaly**: a hypothesis has at least one `part_of` claim
whose computed `epistemic_status` is `contradicted` or `contested` (`knowledge_base/status.py`), which by that
module's algebra requires an *active* `contradicts` — a verified relation from a verified, current claim. This
script replays that condition over the archived knowledge base, so the decision to build the phase rests on a
number read from persisted artifacts rather than on an expectation.

    uv run python -m evaluation.refine_entry [--kb ../knowledgebase] [--format markdown]

Two counts are reported per run, and they should agree:

- **as of verify** — the condition evaluated against only the records that existed when that run's `verify`
  phase finished, which is what a `refine` phase would actually have seen. The cutoff is the `created_at` of the
  last record written by an agent whose id starts with `verifier`; a run with no such record is reported as
  `null`, not as zero.
- **on today's knowledge base** — the same condition over every record. The knowledge base is shared across
  runs, so a later run can contradict an earlier run's claim; where the two counts differ, the difference is
  evidence of exactly that, not an error.

What this does not do: judge. It counts, and it names the hypotheses and the claims, so the decision is
reviewable.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from knowledge_base.schemas import Event, Hypothesis, Relation
from knowledge_base.status import statuses, superseded, visible_at
from knowledge_base.store import read_records
from knowledge_base.validate import Record

DEFAULT_KB: Final = Path(__file__).resolve().parents[1] / "knowledgebase"
ANOMALOUS: Final = ("contradicted", "contested")
VERIFIER: Final = "verifier"


def verify_cutoff(records: Mapping[str, Record], run_id: str) -> datetime | None:
    """When the run's `verify` phase last wrote. `None` when it wrote nothing: a phase that produced no record
    leaves no trace here, and saying so is not the same as saying zero."""
    written = [
        r.created_at
        for r in records.values()
        if isinstance(r, Event) and r.run_id == run_id and (r.agent_id or "").startswith(VERIFIER)
    ]
    return max(written) if written else None


def claims_of(records: Mapping[str, Record]) -> dict[str, list[str]]:
    """Hypothesis ID -> the claims linked to it by `part_of`."""
    parts: dict[str, list[str]] = {}
    for record in records.values():
        if isinstance(record, Relation) and record.type == "part_of":
            parts.setdefault(record.target_id, []).append(record.source_id)
    return parts


def entering(records: Mapping[str, Record], run_id: str, parts: Mapping[str, list[str]]) -> list[dict[str, Any]]:
    """The hypotheses of `run_id`, visible in `records`, that the entry condition admits."""
    status = statuses(records)
    old = superseded(records)
    admitted = []
    for hypothesis in records.values():
        if not isinstance(hypothesis, Hypothesis) or hypothesis.run_id != run_id or hypothesis.id in old:
            continue
        own = [c for c in parts.get(hypothesis.id, []) if c in records]
        anomalous = [c for c in own if status.get(c) in ANOMALOUS]
        if anomalous:
            admitted.append(
                {"hypothesis": hypothesis.id, "claims": len(own), "anomalous": {c: status[c] for c in anomalous}}
            )
    return admitted


def confirmed_by_verify(records: Mapping[str, Record], run_id: str) -> dict[str, int]:
    """What the run's `verify` phase confirmed, by relation type. A confirmed verdict writes a new verified
    relation whose rationale begins with `confirmed by` (`pipeline/proposals.py::Builder.confirmed`), so this
    reads the result of verification rather than the verdicts, which live only in the phase report."""
    counts: dict[str, int] = {}
    for record in records.values():
        if (
            isinstance(record, Relation)
            and record.run_id == run_id
            and record.provenance_status == "verified"
            and (record.rationale or "").startswith("confirmed by")
        ):
            counts[record.type] = counts.get(record.type, 0) + 1
    return counts


def report(records: Mapping[str, Record]) -> dict[str, Any]:
    parts = claims_of(records)
    runs = sorted({r.run_id for r in records.values() if isinstance(r, Hypothesis)})
    rows = []
    for run_id in runs:
        hypotheses = [r for r in records.values() if isinstance(r, Hypothesis) and r.run_id == run_id]
        cutoff = verify_cutoff(records, run_id)
        if cutoff is None:
            as_of: list[dict[str, Any]] | None = None
        else:
            as_of = entering(visible_at(records, cutoff), run_id, parts)
        rows.append(
            {
                "run_id": run_id,
                "hypotheses": len(hypotheses),
                "verify_cutoff": cutoff.isoformat() if cutoff else None,
                "verify_confirmed": confirmed_by_verify(records, run_id),
                "as_of_verify": as_of,
                "today": entering(records, run_id, parts),
            }
        )
    measured = [r for r in rows if r["as_of_verify"] is not None]
    return {
        "runs": rows,
        "totals": {
            "hypotheses": sum(r["hypotheses"] for r in rows),
            "runs": len(rows),
            "runs_without_a_verify_record": len(rows) - len(measured),
            "entering_as_of_verify": sum(len(r["as_of_verify"]) for r in measured),
            "entering_today": sum(len(r["today"]) for r in rows),
        },
    }


def render(data: dict[str, Any]) -> str:
    lines = [
        "# Would the `refine` entry condition fire?",
        "",
        "| run | hypotheses | verify confirmed | would enter (as of verify) | would enter (KB today) |",
        "|---|---:|---|---:|---:|",
    ]
    for row in data["runs"]:
        confirmed = ", ".join(f"{t} {n}" for t, n in sorted(row["verify_confirmed"].items())) or "nothing"
        as_of = "no verify record" if row["as_of_verify"] is None else str(len(row["as_of_verify"]))
        lines.append(f"| {row['run_id']} | {row['hypotheses']} | {confirmed} | {as_of} | {len(row['today'])} |")
    totals = data["totals"]
    lines += [
        "",
        f"**{totals['entering_as_of_verify']} of {totals['hypotheses']} hypotheses** would have "
        f"entered as of their run's `verify`; {totals['entering_today']} would on today's knowledge "
        f"base. {totals['runs_without_a_verify_record']} run(s) wrote no record in `verify` and are "
        f"not counted.",
        "",
    ]
    for row in data["runs"]:
        for item in row["as_of_verify"] or []:
            claims = ", ".join(f"{c} ({s})" for c, s in item["anomalous"].items())
            lines.append(
                f"- {row['run_id']} {item['hypothesis']}: {len(item['anomalous'])} of "
                f"{item['claims']} claims anomalous — {claims}"
            )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--kb", type=Path, default=DEFAULT_KB, help=f"knowledge base (default {DEFAULT_KB})")
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    args = parser.parse_args(argv)
    data = report(read_records(args.kb))
    print(render(data) if args.format == "markdown" else json.dumps(data, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
