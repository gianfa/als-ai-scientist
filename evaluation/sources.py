"""The register of scientific sources a run read, written from the run's own archive.

A run leaves its reading in four separate places: `literature-calls.jsonl` (every quote check,
with the paper, the hash of the text it was checked against and a receipt), the per-phase `fetched` map,
the `source_refs` of the claims it wrote, and the MCP configuration naming the extracted paper the
`paper` server served. The catalogue itself — title, year, licence, access date, hash — lives in the
literature store's index, which is shared across runs.

This module joins them into one file per run, `results/sources-read.json`, so the question "what did this
run actually read, and under what licence" has a single answer that a reader can check.

It is deterministic and derivative: no model is called, nothing is fetched, and every field is copied
from an artifact the run already persisted. Where the artifacts disagree or an identifier cannot be
resolved, that is reported as a finding rather than smoothed over — a source the run cited and the store
has never seen is exactly the thing a reader needs to know about.

    uv run python -m evaluation.sources <run-id> [--kb knowledgebase] [--store literature/store]
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from knowledge_base.store import read_records
from pipeline.provenance import fetch_outcome
from tools.literature.store import INTERNAL_REF_PREFIXES, canonical_id

REPO_ROOT = Path(__file__).resolve().parents[1]
INTERNAL_PREFIXES = INTERNAL_REF_PREFIXES  # re-exported: the one definition lives in the store


class RunArtifactsMissing(RuntimeError):
    """The run directory does not hold the artifacts this register is derived from."""


# The one canonicaliser lives in `tools/literature/store.py`, next to the store that owns the aliases, and
# the host now calls it when it writes a claim. This module had a second copy of it for two days, which is
# exactly the fault the project paid for with two report sources that could disagree.
normalise = canonical_id


def load_store(index_path: Path) -> tuple[dict[str, dict], dict[str, str]]:
    """The literature store's catalogue, plus a map from every alias to its canonical identifier."""
    catalogue: dict[str, dict] = {}
    alias_of: dict[str, str] = {}
    if not index_path.exists():
        return catalogue, alias_of
    for line in index_path.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        canonical = normalise(row.get("canonical_id", "")) or row.get("canonical_id", "")
        catalogue[canonical] = row
        for alias in [row.get("canonical_id"), *(row.get("aliases") or [])]:
            key = normalise(alias or "")
            if key:
                alias_of[key] = canonical
    return catalogue, alias_of


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _paper_tool_target(results: Path) -> str | None:
    """Which extracted paper the `paper` MCP server was pointed at, read from a phase's own config."""
    for config in sorted(results.glob("workflows/*.mcp.json")):
        args = (json.loads(config.read_text()).get("mcpServers", {}).get("paper") or {}).get("args") or []
        if "--paper" in args:
            return args[args.index("--paper") + 1]
    return None


def build(run_id: str, results: Path, kb_root: Path, store_index: Path) -> dict[str, Any]:
    if not results.is_dir():
        raise RunArtifactsMissing(f"no results directory for {run_id}: {results}")

    catalogue, alias_of = load_store(store_index)
    resolve = lambda key: alias_of.get(key, key)  # noqa: E731 - a name would not make this clearer

    touched: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "cited_by_claims": [],
            "quote_checks": Counter(),
            "receipts": [],
            "fetched_during_run": None,
            "as_written": set(),
        }
    )
    unresolved: Counter[str] = Counter()

    def note(raw: str) -> str | None:
        key = normalise(raw)
        if key is None:
            unresolved[raw.strip()] += 1
            return None
        canonical = resolve(key)
        touched[canonical]["as_written"].add(raw.strip())
        return canonical

    # 1. Quote checks: the only record of a source being *read against a claim*.
    for call in _read_jsonl(results / "literature-calls.jsonl"):
        canonical = note(call.get("paper") or call.get("paper_arg") or "")
        if canonical is None:
            continue
        entry = touched[canonical]
        entry["quote_checks"][call.get("verdict") or call.get("status") or "unknown"] += 1
        if receipt := call.get("receipt_id"):
            entry["receipts"].append(receipt)

    # 2. Papers the run fetched, with the outcome the phase recorded.
    for report in sorted(results.glob("[0-9][0-9]-*-report.json")):
        fetched = json.loads(report.read_text()).get("fetched")
        if isinstance(fetched, dict):
            for raw, value in fetched.items():
                # The value is {outcome, reason}; older archives carry the bare outcome.
                outcome, reason = fetch_outcome(value)
                if canonical := note(raw):
                    touched[canonical]["fetched_during_run"] = outcome
                    if reason:
                        touched[canonical]["fetch_reason"] = reason

    # 3. Sources the run's own claims cite. Internal references are provenance of a different kind.
    for record in read_records(kb_root).values():
        if getattr(record, "run_id", None) != run_id:
            continue
        for ref in getattr(record, "source_refs", None) or []:
            if ref.startswith(INTERNAL_PREFIXES):
                continue
            if canonical := note(ref):
                touched[canonical]["cited_by_claims"].append(record.id)

    sources = []
    for canonical, entry in sorted(touched.items()):
        row = catalogue.get(canonical)
        sources.append(
            {
                "id": canonical,
                "cited_as": sorted(entry["as_written"]),
                "title": (row or {}).get("title"),
                "year": (row or {}).get("year"),
                "licence": (row or {}).get("licence"),
                "source_url": (row or {}).get("source_url"),
                "in_store": row is not None,
                "store_status": (row or {}).get("status"),
                "text_sha256": (row or {}).get("sha256"),
                "access_date": (row or {}).get("access_date"),
                "first_seen_run": (row or {}).get("run_id"),
                "cited_by_claims": sorted(entry["cited_by_claims"]),
                "quote_checks": dict(entry["quote_checks"]),
                "quote_receipts": sorted(entry["receipts"]),
                "fetched_during_run": entry["fetched_during_run"],
            }
        )

    cited = [s for s in sources if s["cited_by_claims"]]
    missing = [s["id"] for s in sources if not s["in_store"]]
    no_text = [s["id"] for s in sources if s["in_store"] and s["store_status"] != "full_text"]
    return {
        "run_id": run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "generated_by": "evaluation/sources.py",
        "derived_from": [
            "literature-calls.jsonl",
            "NN-<phase>-report.json (fetched)",
            "the knowledge base's source_refs for this run",
            "literature/store/index.jsonl",
            "workflows/NN-<phase>.mcp.json",
        ],
        "note": (
            "Sources this run touched, joined from its own archive. A source appears here because the "
            "run quote-checked it, fetched it, or cited it in a claim. Being listed says nothing about "
            "whether the claim resting on it is sound."
        ),
        "paper_tool_served": _paper_tool_target(results),
        "counts": {
            "sources": len(sources),
            "cited_in_claims": len(cited),
            "quote_checked": sum(1 for s in sources if s["quote_checks"]),
            "fetched_during_run": sum(1 for s in sources if s["fetched_during_run"]),
            "absent_from_store": len(missing),
            "in_store_without_full_text": len(no_text),
            "licences": dict(Counter(s["licence"] for s in sources if s["licence"])),
        },
        "absent_from_store": missing,
        "in_store_without_full_text": no_text,
        "unresolved_identifiers": dict(unresolved),
        "sources": sources,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("run_id", nargs="+")
    parser.add_argument("--experiments", type=Path, default=REPO_ROOT / "experiments")
    parser.add_argument("--kb", type=Path, default=REPO_ROOT / "knowledgebase")
    parser.add_argument("--store", type=Path, default=REPO_ROOT / "literature" / "store")
    parser.add_argument("--out", type=Path, default=None, help="write here instead of <run>/results/sources-read.json")
    args = parser.parse_args(argv)

    failed = 0
    for run_id in args.run_id:
        results = args.experiments / run_id / "results"
        try:
            register = build(run_id, results, args.kb, args.store / "index.jsonl")
        except RunArtifactsMissing as exc:
            print(f"{run_id}: skipped — {exc}")
            failed += 1
            continue
        path = args.out or (results / "sources-read.json")
        path.write_text(json.dumps(register, indent=2) + "\n")
        counts = register["counts"]
        print(
            f"{run_id}: {path} — {counts['sources']} sources, {counts['cited_in_claims']} cited, "
            f"{counts['absent_from_store']} absent from the store"
        )
    return 1 if failed and failed == len(args.run_id) else 0


if __name__ == "__main__":
    raise SystemExit(main())
