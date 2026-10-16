"""Rendering a knowledge-base graph: Graphviz for a picture, Mermaid for a document.

Colour carries the one thing a reader needs and cannot infer: how well a claim is supported. A claim nobody has
verified looks different from one verified evidence contradicts, because treating them alike on a page is how a
reader comes to believe an unresolved edge.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

SHAPE: dict[str, str] = {
    "entity": "ellipse",
    "claim": "box",
    "hypothesis": "hexagon",
    "ignorance": "note",
    "experiment": "component",
    "intervention": "cds",
    "decision": "diamond",
}
# Epistemic status first (claims), then record kind.
FILL: dict[str, str] = {
    "supported": "#cfe9cf",
    "contradicted": "#f2c7c7",
    "contested": "#f6e2b3",
    "unresolved": "#ffffff",
    "entity": "#e8eef7",
    "hypothesis": "#ede3f7",
    "ignorance": "#f7f3dc",
    "experiment": "#dcf0f7",
    "intervention": "#f7e6dc",
    "decision": "#eeeeee",
}
EDGE_STYLE: dict[str, str] = {
    "contradicts": 'color="#b23b3b"',
    "supports": 'color="#3b7a3b"',
    "questions": "style=dashed",
    "supersedes": "style=dotted",
    "cause": 'arrowhead=none, color="#777777"',
    "effect": 'color="#777777"',
    "mentions": 'style=dotted, color="#aaaaaa"',
    "buys": "style=bold",
}
MAX_LABEL = 60


def _short(text: str, limit: int = MAX_LABEL) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _quote(text: str) -> str:
    return str(text).replace("\\", "\\\\").replace('"', '\\"')


def to_dot(graph: Mapping[str, Any], name: str = "knowledge_base") -> str:
    lines = [
        f'digraph "{_quote(name)}" {{',
        "  rankdir=LR;",
        '  node [fontname="Helvetica", fontsize=10];',
        '  edge [fontname="Helvetica", fontsize=9];',
    ]
    for node in graph["nodes"]:
        fill = FILL.get(node.get("epistemic_status") or node["kind"], "#ffffff")
        label = f'{node["id"]}\\n{_quote(_short(node["label"]))}'
        style = "filled,dashed" if node.get("superseded") else "filled"
        lines.append(
            f'  "{_quote(node["id"])}" [label="{label}", shape={SHAPE.get(node["kind"], "box")}, '
            f'style="{style}", fillcolor="{fill}"];'
        )
    for edge in graph["edges"]:
        attrs = EDGE_STYLE.get(edge["kind"], "")
        unverified = edge.get("provenance_status") == "unverified"
        attrs = ", ".join(
            x
            for x in (
                f'label="{_quote(edge["kind"])}"',
                attrs,
                "style=dashed" if unverified and "style" not in attrs else "",
            )
            if x
        )
        lines.append(f'  "{_quote(edge["source"])}" -> "{_quote(edge["target"])}" [{attrs}];')
    lines.append("}")
    return "\n".join(lines) + "\n"


MERMAID_BRACKETS: dict[str, tuple[str, str]] = {
    "entity": ("(", ")"),
    "hypothesis": ("{{", "}}"),
    "decision": ("{", "}"),
    "experiment": ("([", "])"),
    "intervention": ("([", "])"),
    "claim": ("[", "]"),
    "ignorance": (">", "]"),
}


def _mermaid_id(identifier: str) -> str:
    return re.sub(r"\W", "_", identifier)


def to_mermaid(graph: Mapping[str, Any]) -> str:
    """A diagram that renders inside a Markdown document, for the report.

    Same colours as the DOT output: a claim takes the fill of its epistemic status, every other node the fill of its
    kind. Shape also follows the kind, so a hypothesis is never confused with a claim in greyscale.
    """
    lines = ["flowchart LR"]
    used: set[str] = set()
    for node in graph["nodes"]:
        label = _short(node["label"]).replace('"', "'")
        cls = node.get("epistemic_status") if node["kind"] == "claim" and node.get("epistemic_status") else node["kind"]
        open_, close = MERMAID_BRACKETS.get(node["kind"], ("[", "]"))
        lines.append(f'  {_mermaid_id(node["id"])}{open_}"{node["id"]}: {label}"{close}:::{cls}')
        used.add(cls)
        if node.get("superseded"):
            lines.append(f'  class {_mermaid_id(node["id"])} superseded')
            used.add("superseded")
    for edge in graph["edges"]:
        arrow = "-.->" if edge.get("provenance_status") == "unverified" else "-->"
        lines.append(f'  {_mermaid_id(edge["source"])} {arrow}|{edge["kind"]}| {_mermaid_id(edge["target"])}')
    for cls in sorted(used):
        fill = FILL.get(cls, "#ffffff")
        extra = ",stroke-dasharray:4 2" if cls == "superseded" else ""
        lines.append(f"  classDef {cls} fill:{fill},stroke:#444,color:#111111{extra}")
    return "\n".join(lines) + "\n"


__all__ = ["EDGE_STYLE", "FILL", "SHAPE", "to_dot", "to_mermaid"]
