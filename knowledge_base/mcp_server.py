"""Read-only MCP server on the knowledge base, for agents.

Three tools, each reading the KB afresh (lock-free, `read_records`) and answering with the compact form of
`retrieve.as_context` (claims carry their computed epistemic status):

- `kb_get(ids)`                         the records with these IDs;
- `kb_neighbourhood(ids, hops)`         the records within `hops` steps of these IDs (hops <= MAX_HOPS);
- `kb_search(query, limit, types)`      records containing every word of the query (limit <= MAX_LIMIT), among
                                        claims and entities unless `types` names other record types;
- `kb_mechanism_graph(entity, limit)`   the causal edges of the knowledge base, each with its epistemic status;
- `kb_mechanism_path(limit)`            the longest chain of proper edges, plus the unchecked edges that touch it.

The server cannot write: it has no write path to the KB at all. Errors are returned as data
(`{"error": ...}`), never raised to the agent.

    uv run python -m knowledge_base.mcp_server --kb <dir> [--check]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Final

from .retrieve import DEFAULT_SEARCH_TYPES, SEARCHABLE_TYPES, as_context, by_id, keyword, neighbourhood
from .status import mechanism_graph, proper_mechanism
from .store import KnowledgeBaseError, read_records

MAX_HOPS: Final = 2
MAX_LIMIT: Final = 50
# The two type lists as an agent would write them in JSON, for the `kb_search` description.
_DEFAULT_TYPES: Final = ", ".join(f'"{name}"' for name in DEFAULT_SEARCH_TYPES)
_ALL_TYPES: Final = ", ".join(f'"{name}"' for name in SEARCHABLE_TYPES)


def _answer(root: Path, select) -> dict[str, Any]:
    try:
        records = read_records(root)
        return {"records": as_context(select(records), records)}
    except (KeyError, ValueError, KnowledgeBaseError, OSError) as err:
        return {"error": f"{type(err).__name__}: {err}"}


def kb_get(root: Path, ids: list[str]) -> dict[str, Any]:
    return _answer(root, lambda records: by_id(ids, records))


def kb_neighbourhood(root: Path, ids: list[str], hops: int = 1) -> dict[str, Any]:
    if not 0 <= hops <= MAX_HOPS:
        return {"error": f"hops must be between 0 and {MAX_HOPS}"}
    return _answer(root, lambda records: neighbourhood(ids, records, hops))


def kb_search(root: Path, query: str, limit: int = 20, types: list[str] | None = None) -> dict[str, Any]:
    if not 1 <= limit <= MAX_LIMIT:
        return {"error": f"limit must be between 1 and {MAX_LIMIT}"}
    return _answer(root, lambda records: keyword(query, records, limit, types))


def kb_mechanism_graph(root: Path, entity: str = "", limit: int = 40) -> dict[str, Any]:
    """The causal edges the knowledge base currently holds, newest last, optionally those touching one entity.

    An edge is one causal claim: cause, effect, context, mechanism, and the status computed from verified
    relations. It is a graph of what has been *asserted and checked*, not a causal model identified from data,
    and the prompts say so. Recomputed on every call, because a run adds edges as it goes: a snapshot taken at
    the start would be stale by the design phase.
    """
    if not 1 <= limit <= MAX_LIMIT:
        return {"error": f"limit must be between 1 and {MAX_LIMIT}"}
    try:
        records = read_records(root)
        edges = mechanism_graph(records)
        if entity:
            needle = entity.strip().lower()
            edges = [
                e
                for e in edges
                if needle in (e.cause_name.lower(), e.effect_name.lower())
                or needle in (e.cause_id.lower(), e.effect_id.lower())
            ]
        return {
            "edges": [vars(e) for e in edges[-limit:]],
            "total": len(edges),
            "note": "One edge per current causal claim. `epistemic_status` is computed from verified "
            "relations; an unresolved edge is a hypothesis to test, not a fact.",
        }
    except (KeyError, ValueError, KnowledgeBaseError, OSError) as err:
        return {"error": f"{type(err).__name__}: {err}"}


def kb_mechanism_path(root: Path, limit: int = 40) -> dict[str, Any]:
    """The longest chain of proper edges the knowledge base holds, plus the unchecked edges that touch it (ADR 0033).

    `kb_mechanism_graph` answers with a *set* of edges, and a set of edges that happens to be disconnected is a
    list of causes rather than a pathway. A proposal's mechanism section is checked for the difference
    (`pipeline/section_gates.py::check_mechanism`): the proper edges it cites must join, and at most one unchecked
    edge, adjacent to them, may stand beside them. So this tool answers with the chain itself, ordered, made only of
    **proper** edges (quote verified against its source, or supported by a verified relation, and not contradicted
    or contested), and separately with the unchecked edges adjacent to it: the candidates for the hypothesis step.
    An edge that other verified claims contradict or contest is in neither list.
    """
    if not 1 <= limit <= MAX_LIMIT:
        return {"error": f"limit must be between 1 and {MAX_LIMIT}"}
    try:
        records = read_records(root)
        sub = proper_mechanism(records)
        return {
            "backbone": [vars(e) for e in sub.backbone[:limit]],
            "neighbourhood": [vars(e) for e in sub.neighbourhood[:limit]],
            "backbone_total": len(sub.backbone),
            "neighbourhood_total": len(sub.neighbourhood),
            "note": "`backbone` is ordered and connected and holds only proper edges: each edge's effect is the "
            "next edge's cause, so citing its claim IDs cites a chain. `neighbourhood` holds unchecked causal "
            "edges touching the chain: cite at most one of them, as the hypothesis step. Neither is a causal "
            "model identified from data.",
        }
    except (KeyError, ValueError, KnowledgeBaseError, OSError) as err:
        return {"error": f"{type(err).__name__}: {err}"}


def build_server(root: Path):
    """The MCP server object (imports the SDK here, and only here)."""
    from mcp.server.mcpserver import MCPServer

    server = MCPServer("kb")

    @server.tool(name="kb_get", description="Knowledge-base records by ID (C-, H-, I-, L-, N-).")
    def get_tool(ids: list[str]) -> dict[str, Any]:
        return kb_get(root, ids)

    @server.tool(
        name="kb_neighbourhood",
        description=f"Records within `hops` (0..{MAX_HOPS}) steps of the given IDs in the knowledge base.",
    )
    def neighbourhood_tool(ids: list[str], hops: int = 1) -> dict[str, Any]:
        return kb_neighbourhood(root, ids, hops)

    @server.tool(
        name="kb_search",
        description=f"Knowledge-base records containing every word of `query` (limit 1..{MAX_LIMIT}). "
        f"It searches the evidence only: by default types=[{_DEFAULT_TYPES}]. The "
        "system's own working records — hypotheses, declared gaps (Ignorance), proposed "
        "experiments, decisions, interventions — are searched only when you ask for them, "
        'by naming record types in `types`: e.g. types=["Hypothesis", "Experiment"] to '
        f"look for one by name, or types=[{_ALL_TYPES}] to search everything. An "
        "unknown type name gives an error, not an empty result.",
    )
    def search_tool(query: str, limit: int = 20, types: list[str] | None = None) -> dict[str, Any]:
        return kb_search(root, query, limit, types)

    @server.tool(
        name="kb_mechanism_graph",
        description="The causal edges of the knowledge base (cause, effect, mechanism, computed "
        "status), optionally only those touching `entity`. Asserted claims, not an "
        "identified causal model.",
    )
    def graph_tool(entity: str = "", limit: int = 40) -> dict[str, Any]:
        return kb_mechanism_graph(root, entity, limit)

    @server.tool(
        name="kb_mechanism_path",
        description="The longest connected chain of proper causal edges in the knowledge base (quote verified "
        "or supported by a verified relation; contradicted or contested edges left out), ordered, plus the "
        "unchecked edges adjacent to it. Cite the chain's claim IDs and at most one adjacent unchecked edge "
        "when you have to state a mechanism: a set of causal claims that do not join is a list of causes, "
        "not a pathway.",
    )
    def path_tool(limit: int = 40) -> dict[str, Any]:
        return kb_mechanism_path(root, limit)

    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--kb", type=Path, required=True, help="knowledge-base directory (must exist)")
    parser.add_argument("--check", action="store_true", help="only verify that the server could start, then exit")
    args = parser.parse_args(argv)
    try:
        if not args.kb.is_dir():
            raise ValueError(f"knowledge-base directory does not exist: {args.kb}")
        read_records(args.kb)  # an unreadable KB stops the server here
        server = build_server(args.kb)
    except (OSError, ValueError, ImportError, KnowledgeBaseError) as err:
        print(f"kb MCP server: cannot start: {err}", file=sys.stderr)
        return 2
    if args.check:
        print("kb MCP server: ok", file=sys.stderr)
        return 0
    server.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
