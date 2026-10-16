"""Register the papers a user dropped in `literature/inbox/` into the literature store (no LLM, no agent).

Run from the repository root:

    uv run python scripts/data/literature_ingest.py [--inbox DIR] [--sources-csv FILE] [--store DIR] [--offline] [--report FILE]

An identifier comes from `literature/sources.csv` (columns `file,identifier,title,year,alias,note`) or, failing that,
from a DOI in the text confirmed on Europe PMC by the paper's title; otherwise the file is reported as pending and
left untouched. Files are never moved or deleted. Exit code 0 unless the input is unusable (2).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PACKAGE_ROOT))

from tools.literature.fetch import http_transport  # noqa: E402
from tools.literature.ingest import DEFAULT_INBOX, DEFAULT_SOURCES_CSV, ingest_inbox, report  # noqa: E402
from tools.literature.store import DEFAULT_ROOT, LiteratureStore  # noqa: E402


def _offline(_url: str) -> str:
    raise OSError("offline mode: Europe PMC was not asked")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--inbox", type=Path, default=DEFAULT_INBOX)
    parser.add_argument("--sources-csv", type=Path, default=DEFAULT_SOURCES_CSV)
    parser.add_argument("--store", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--offline", action="store_true", help="do not contact Europe PMC (no identifier inference)")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--report", type=Path, default=None, help="write the JSON report here (never overwritten)")
    args = parser.parse_args(argv)
    try:
        store = LiteratureStore(args.store)
        results = ingest_inbox(
            args.inbox, args.sources_csv, store, _offline if args.offline else http_transport, run_id=args.run_id
        )
    except (OSError, ValueError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 2
    summary = report(results)
    if any(r.outcome == "ingested" for r in results):
        store.render_index_md()
    for entry in summary["entries"]:
        if entry["outcome"] in ("pending_identifier", "failed_extraction", "missing_file", "unsupported_type"):
            print(f"{entry['outcome']}: {entry['file']}: {entry['detail']}", file=sys.stderr)
    if args.report is not None:
        if args.report.exists():
            print(f"error: {args.report} exists; not overwriting", file=sys.stderr)
            return 2
        args.report.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary["counts"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
