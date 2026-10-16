"""Build a graph from a knowledge base and write it.

    uv run python -m tools.graph --kb ../knowledgebase --format dot --out graph.dot
    uv run python -m tools.graph --kb ../knowledgebase --run <run-id> --format mermaid
    uv run python -m tools.graph --kb ../knowledgebase --focus H-14 --hops 2 --format json

Read-only: it never writes to the knowledge base and never overwrites an existing output file.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from knowledge_base.store import KnowledgeBaseError, read_records

from .build import build
from .render import to_dot, to_mermaid
from .svg import to_html, to_svg


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--kb", type=Path, required=True, help="knowledge-base directory")
    parser.add_argument("--run", default=None, help="keep only the records of this run")
    parser.add_argument("--focus", nargs="*", default=None, help="record IDs to centre the graph on")
    parser.add_argument("--hops", type=int, default=1, help="neighbourhood depth around --focus")
    parser.add_argument("--include-superseded", action="store_true")
    parser.add_argument(
        "--format",
        choices=("json", "dot", "mermaid", "svg", "html"),
        default="json",
        help="svg and html are self-contained and need nothing installed; dot needs Graphviz "
        "and mermaid a Markdown viewer",
    )
    parser.add_argument("--title", default=None, help="title of the HTML page")
    parser.add_argument("--out", type=Path, default=None, help="write to this file instead of stdout")
    args = parser.parse_args(argv)
    if not args.kb.is_dir():
        # An empty graph for a mistyped path reads as "the knowledge base is empty", which is worse than an error.
        print(f"tools.graph: cannot read the knowledge base: no such directory: {args.kb}", file=sys.stderr)
        return 2
    try:
        records = read_records(args.kb)
    except (OSError, ValueError, KnowledgeBaseError) as err:
        print(f"tools.graph: cannot read the knowledge base: {err}", file=sys.stderr)
        return 2
    graph = build(
        records, run_id=args.run, focus=args.focus, hops=args.hops, include_superseded=args.include_superseded
    )
    title = args.title or f"Knowledge-base graph — {args.run or args.kb.name}"
    renderers = {
        "json": lambda g: json.dumps(g, indent=2) + "\n",
        "dot": to_dot,
        "mermaid": to_mermaid,
        "svg": to_svg,
        "html": lambda g: to_html(g, title),
    }
    text = renderers[args.format](graph)
    if args.out is None:
        print(text, end="")
        return 0
    if args.out.exists():
        print(f"tools.graph: refusing to overwrite {args.out}", file=sys.stderr)
        return 2
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text)
    print(f"tools.graph: {len(graph['nodes'])} nodes, {len(graph['edges'])} edges -> {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
