"""Read-only MCP server on a run's data manifest, for agents.

Without it no agent can know which datasets a run has: the prompts name the knowledge-base tools and the web
budget, never the data, so a hypothesis author cannot see that flow cytometry is available. Two tools close
that gap:

- `data_list()`                 every source the `data` phase recorded, with kind, size and licence note;
- `data_describe(source_id)`    one source, with the analyses configured to consume it.

**Metadata only.** The server never returns the contents of a file and has no tool that could: reading a data file
is the job of code that writes a verifiable artifact, and a claim must not rest on an agent's
reading of a dataset. What a dataset contains is said by the `compute` phase, afterwards, or not at all. The
server cannot write either: it has no write path to the manifest or the knowledge base.

Errors are returned as data (`{"error": ...}`), never raised to the agent.

    uv run python -m tools.data.mcp_server --manifest <run>/data-manifest.json [--check]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Final

# Manifest keys an agent may see. `path` is deliberately absent: a path invites a read, and reading is not an
# agent's job here. The reference is what a later record would cite.
# `shape` is what the source *measures*, when the files say so (`local_sources._shape`). It is here because
# an agent choosing a dataset needs it: without it an experiment can rest on a cytokine the
# named deposit never recorded. Still metadata, never file contents.
PUBLIC_FIELDS: Final = ("id", "kind", "ref", "files", "bytes", "is_directory", "modified_at", "license_note", "shape")


def _load(manifest_path: Path) -> dict[str, Any]:
    return json.loads(manifest_path.read_text())


def _public(entry: dict[str, Any]) -> dict[str, Any]:
    return {key: entry[key] for key in PUBLIC_FIELDS if key in entry}


def _consumers(source_id: str) -> list[str]:
    """The analyses declared to read this source (`pipeline.compute.ANALYSIS_SOURCES`), so an agent can tell a
    dataset already analysed from one nothing reads."""
    from pipeline.compute import ANALYSIS_SOURCES

    return sorted(analysis for analysis, sources in ANALYSIS_SOURCES.items() if source_id in sources)


def data_list(manifest_path: Path) -> dict[str, Any]:
    """Every recorded source, plus the ones the run declared but did not find."""
    try:
        data = _load(manifest_path)
        return {
            "sources": [_public(entry) for entry in data.get("sources", [])],
            "absent": [entry.get("id") for entry in data.get("absent", [])],
            "bytes": data.get("bytes"),
            "files": data.get("files"),
            "note": "Metadata only: file contents are not available to agents. Computed results come from the "
            "`compute` phase.",
        }
    except (OSError, ValueError, KeyError) as err:
        return {"error": f"{type(err).__name__}: {err}"}


def data_describe(manifest_path: Path, source_id: str) -> dict[str, Any]:
    """One source. An id the manifest does not hold is an error, not an empty answer: an agent must not conclude
    that a dataset is absent because it misspelled its name."""
    try:
        data = _load(manifest_path)
        for entry in data.get("sources", []):
            if entry.get("id") == source_id:
                return {"source": _public(entry), "analysed_by": _consumers(source_id)}
        absent = [e.get("id") for e in data.get("absent", [])]
        if source_id in absent:
            return {
                "source": {"id": source_id},
                "absent": True,
                "note": "Declared by the configuration but not found on disk in this run.",
            }
        known = [e.get("id") for e in data.get("sources", [])]
        return {"error": f"unknown source {source_id!r}; this run recorded {known + absent}"}
    except (OSError, ValueError, KeyError) as err:
        return {"error": f"{type(err).__name__}: {err}"}


def build_server(manifest_path: Path):
    """The MCP server object (imports the SDK here, and only here)."""
    from mcp.server.mcpserver import MCPServer

    server = MCPServer("data")

    @server.tool(
        name="data_list",
        description="The datasets this run has: id, kind, size, licence note. Metadata only, never file " "contents.",
    )
    def list_tool() -> dict[str, Any]:
        return data_list(manifest_path)

    @server.tool(
        name="data_describe",
        description="One dataset of this run by id, with the analyses configured to read it. Metadata "
        "only, never file contents.",
    )
    def describe_tool(source_id: str) -> dict[str, Any]:
        return data_describe(manifest_path, source_id)

    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest", type=Path, required=True, help="data-manifest.json of the run (must exist)")
    parser.add_argument("--check", action="store_true", help="only verify that the server could start, then exit")
    args = parser.parse_args(argv)
    try:
        if not args.manifest.is_file():
            raise ValueError(f"data manifest does not exist: {args.manifest}")
        _load(args.manifest)  # an unreadable manifest stops the server here
        server = build_server(args.manifest)
    except (OSError, ValueError, ImportError) as err:
        print(f"data MCP server: cannot start: {err}", file=sys.stderr)
        return 2
    if args.check:
        print("data MCP server: ok", file=sys.stderr)
        return 0
    server.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
