"""Fetch the open-access full texts of a reading list from Europe PMC into the literature store.

Run from the repository root:

    uv run python scripts/data/literature_fetch.py --reading-list FILE [--store DIR] [--max-new N] [--report FILE]

`FILE` is a JSON list of identifiers (DOI, PMID, PMCID) or objects with an `identifier` key, or an object
with a `reading_list` list. At most `--max-new` (default 5) papers are newly downloaded; cached, paywalled
and unresolvable entries do not count. Nothing is fetched that is not on the list. Exit code 0 even when
some entries are not available (the report says which); non-zero only for bad input.

`--openalex` adds the OpenAlex fallback for papers Europe PMC cannot serve: `publishedVersion` PDFs only, stored
under the store's `pdf/` (see `tools/literature/fetch.py`).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PACKAGE_ROOT))

from tools.literature.fetch import (
    DEFAULT_MAX_NEW,
    fetch_reading_list,
    http_bytes_transport,  # noqa: E402
    http_transport,
    load_reading_list,
    report,
)
from tools.literature.store import DEFAULT_ROOT, LiteratureStore  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--reading-list", type=Path, required=True)
    parser.add_argument("--store", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--max-new", type=int, default=DEFAULT_MAX_NEW)
    parser.add_argument("--retry-unavailable", action="store_true")
    parser.add_argument("--openalex", action="store_true", help="fall back to OpenAlex publishedVersion PDFs")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--report", type=Path, default=None, help="write fetch-report.json here (never overwritten)")
    args = parser.parse_args(argv)
    try:
        entries = load_reading_list(args.reading_list)
        store = LiteratureStore(args.store)
        results = fetch_reading_list(
            entries,
            store,
            http_transport,
            max_new=args.max_new,
            run_id=args.run_id,
            retry_unavailable=args.retry_unavailable,
            pdf_transport=http_bytes_transport if args.openalex else None,
        )
    except (OSError, ValueError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 2
    store.render_index_md()
    summary = report(results)
    if args.report is not None:
        if args.report.exists():
            print(f"error: {args.report} exists; not overwriting", file=sys.stderr)
            return 2
        args.report.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary["counts"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
