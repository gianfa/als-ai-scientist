#!/usr/bin/env python3
"""Re-point literature-store records whose text is on disk under a path that no longer resolves.

Reports by default and writes only with `--apply`. See `LiteratureStore.relink_paths` for the rule: a record
is corrected only when the file is present and hashes to the sha256 the record already declares, and the
correction is appended, never written over the existing entry.

    uv run python scripts/ops/literature-store-relink.py                 # report
    uv run python scripts/ops/literature-store-relink.py --apply         # append the corrections
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.literature.store import DEFAULT_ROOT, LiteratureStore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--store", type=Path, default=DEFAULT_ROOT, help=f"store root (default {DEFAULT_ROOT})")
    parser.add_argument("--apply", action="store_true", help="append the corrected records instead of reporting")
    parser.add_argument("--json", action="store_true", help="print the full report as JSON")
    parser.add_argument(
        "--check",
        action="store_true",
        help="only report the store's integrity: does every text resolve and still hash?",
    )
    args = parser.parse_args(argv)

    store = LiteratureStore(args.store)
    if not store.index_path.is_file():
        print(f"no index at {store.index_path}", file=sys.stderr)
        return 2
    if args.check:
        report = store.integrity()
        print(
            json.dumps(report, indent=2, sort_keys=True)
            if args.json
            else f"{store.index_path}: {report['papers']} papers, {report['readable']} readable, "
            f"{len(report['missing'])} missing, {len(report['mismatched'])} mismatched -- "
            f"{'sound' if report['sound'] else 'NOT SOUND'}"
        )
        return 0 if report["sound"] else 1
    report = store.relink_paths(dry_run=not args.apply)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    sound = store.integrity()
    print(
        f"{store.index_path}: {sound['papers']} papers, {sound['readable']} readable, "
        f"{len(sound['no_text_recorded'])} with no text recorded"
        f" -- {'sound' if sound['sound'] else 'NOT SOUND'}"
    )
    for label in ("missing", "mismatched"):
        for row in sound[label]:
            print(f"  integrity {label}: {row['paper']} {row['path']}")
    verb = "relinked" if args.apply else "would relink"
    print(f"{verb}: {len(report['relinked'])}")
    for paper, fields in sorted(report["relinked"].items()):
        print(f"  {paper}: " + ", ".join(f"{k} -> {v}" for k, v in sorted(fields.items())))
    for label in ("missing", "mismatched", "outside_store"):
        rows = report[label]
        if rows:
            print(f"{label}: {len(rows)}")
            for row in rows:
                print(f"  {row['paper']} {row['field']} {row['path']}")
    if not args.apply and report["relinked"]:
        print("\nnothing was written; re-run with --apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
