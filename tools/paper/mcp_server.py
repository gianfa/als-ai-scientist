"""MCP server (stdio) with two tools over the Paper2Agent extraction: `paper_navigate` and `paper_passage`.

Why a server at all: the extraction is a pile of markdown under `dist/`, the skill that ships with it is an
`rg`/`sed` protocol, agents have no shell, and a phase session is confined to its stage directory, so a `Read`
of `dist/` is denied. Without this server the agents have no route to the paper's text at all — `check_quote` verifies a quotation they already
have, it cannot be used to read.

Design rules, each of them load-bearing:

- **Read-only, one file.** The server is given one path and never leaves it. The extraction is local-only
  material governed by the source paper's rights (`LICENSES.md`): nothing here publishes or packages it.
- **Bounded.** A request wider than 60 lines is refused, not truncated: an agent cannot tell a silently
  shortened answer from a complete one.
- **Located.** Every answer says whether its lines lie outside the paper's main text, where a quotation can
  never verify `exact`. The flag is a documented lower bound (`document.py`); `check_quote` stays the gate.
- **Provenance in every answer.** The served file's `sha256` and the exact line range, so a reader can check
  any served passage against the file.
- **Errors are data.** The tools never raise; a request that cannot be served comes back `status: "refused"`
  with a `reason`, so "refused" is never confused with "not in the paper".

Run (from the repository root): `uv run python -m tools.paper.mcp_server --paper FILE` (add `--check` to only
verify it can start). Diagnostics go to stderr; stdout belongs to the protocol.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Final

from tools.paper.document import MAX_PASSAGE_LINES, PaperDocument

TOOL_VERSION: Final = "1"

NAVIGATE_DESCRIPTION: Final = (
    "The paper's table of contents: every heading with its line range, its size in characters, and whether the "
    "section lies outside the paper's main text (a table, a reference list, the converter's own notes). Call "
    "this before paper_passage, to choose what to read. The answer carries the source file's sha256."
)

PASSAGE_DESCRIPTION: Final = (
    "A passage of the paper, by heading or by line range, with its markup removed. At most "
    f"{MAX_PASSAGE_LINES} lines: a wider request is refused rather than shortened, so ask for a subsection. "
    "The answer carries the source file's sha256, the exact line range and the enclosing heading. "
    "A quotation you take from a passage must still be verified with mcp__literature__check_quote, which is "
    "the only thing that makes it evidence. When 'outside_main_text' is true the passage is a caption, table, "
    "reference or front matter: the text is real, but check_quote cannot return 'exact' for it, so paraphrase "
    "it or cite the dataset reference from the data manifest instead of quoting it."
)


def build_server(document: PaperDocument):
    """The MCP server object (imports the SDK here, and only here, so the core stays testable without it)."""
    from mcp.server.mcpserver import MCPServer

    server = MCPServer("paper")

    @server.tool(name="paper_navigate", description=NAVIGATE_DESCRIPTION)
    def paper_navigate_tool() -> dict[str, Any]:
        return document.navigate()

    @server.tool(name="paper_passage", description=PASSAGE_DESCRIPTION)
    def paper_passage_tool(
        heading: str | None = None, start: int | None = None, end: int | None = None
    ) -> dict[str, Any]:
        return document.passage(heading=heading, start=start, end=end)

    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--paper", type=Path, required=True, help="the extraction's markdown file (must exist; read-only)"
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="only verify that the server could start (file readable, SDK importable), then " "exit; writes nothing",
    )
    args = parser.parse_args(argv)
    try:
        if not args.paper.is_file():
            raise ValueError(f"paper file does not exist: {args.paper}")
        document = PaperDocument(args.paper)
        if not document.sections:
            raise ValueError(f"no headings found in {args.paper}: this is not the extraction's markdown")
        server = build_server(document)  # imports the SDK: fails here if it is missing
        if args.check:
            print(
                f"paper MCP server: ok ({len(document.sections)} sections, sha256 {document.sha256[:12]})",
                file=sys.stderr,
            )
            return 0
    except (OSError, ValueError, ImportError, UnicodeDecodeError) as err:
        print(f"paper MCP server: cannot start: {err}", file=sys.stderr)
        return 2
    server.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
