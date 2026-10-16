"""A picture of a knowledge-base graph, with no dependency at all.

The previous architecture drew its causal graph as inline SVG in the observer UI
(`observability/wf_observer/static/js/run/causal_graph.js`): a hand-computed circular layout, no D3, no Graphviz,
so it worked offline. This module keeps that property and drops the browser: it writes an SVG, or an HTML page
holding it, that opens anywhere and needs nothing installed. This container has neither `dot` nor a Mermaid
renderer, so a `.dot` file here is text nobody can see.

Isolated on purpose: pure functions, no I/O, no other renderer imported except the shared colour table in
`render.py`, so `--format dot`, `mermaid`, `svg` and `html` cannot break one another.

What the picture asserts, and what it does not: an edge is a record someone wrote, a colour is the status computed
from verified relations, and a dashed edge is an inference nobody has verified. Drawing an unverified edge like a
verified one is how a reader comes to believe it, so they are drawn differently.

The layout is a deterministic ellipse, ordered by kind then ID: the same graph always yields the same picture,
which matters for a figure in a report. It is readable for a focused neighbourhood (`--focus ID --hops 1`) and
unreadable past about forty nodes, which is a property of circular layouts and is stated rather than hidden.
"""

from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

from .render import FILL, MAX_LABEL, _short

WIDTH, HEIGHT = 1100, 760
NODE_W, NODE_H = 150, 42
MARGIN = 90
KIND_ORDER = ("entity", "claim", "hypothesis", "ignorance", "experiment", "intervention", "decision")
EDGE_COLOUR = {
    "supports": "#2f6b3a",
    "contradicts": "#a33a2a",
    "questions": "#8a7a2a",
    "tests": "#2a5f8a",
    "buys": "#6a3a8a",
    "cause": "#777777",
    "effect": "#777777",
    "mentions": "#bbbbbb",
}


def _escape(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def layout(nodes: Sequence[Mapping[str, Any]]) -> dict[str, tuple[float, float]]:
    """Positions on an ellipse, ordered by kind then ID. Deterministic: a figure must not move between runs."""
    ordered = sorted(
        nodes, key=lambda n: (KIND_ORDER.index(n["kind"]) if n["kind"] in KIND_ORDER else len(KIND_ORDER), str(n["id"]))
    )
    cx, cy = WIDTH / 2, HEIGHT / 2
    rx, ry = cx - MARGIN - NODE_W / 2, cy - MARGIN - NODE_H / 2
    count = max(len(ordered), 1)
    return {
        str(n["id"]): (
            cx + rx * math.cos(2 * math.pi * i / count - math.pi / 2),
            cy + ry * math.sin(2 * math.pi * i / count - math.pi / 2),
        )
        for i, n in enumerate(ordered)
    }


def _edge_line(edge: Mapping[str, Any], at: Mapping[str, tuple[float, float]]) -> str:
    x1, y1 = at[str(edge["source"])]
    x2, y2 = at[str(edge["target"])]
    colour = EDGE_COLOUR.get(edge["kind"], "#999999")
    dashed = edge.get("provenance_status") == "unverified"
    dash = ' stroke-dasharray="6 4"' if dashed else ""
    status = " (unverified inference)" if dashed else ""
    title = _escape(f'{edge["source"]} -{edge["kind"]}-> {edge["target"]}{status}')
    return (
        f"    <g><title>{title}</title>"
        f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" stroke="{colour}" '
        f'stroke-width="1.6"{dash} marker-end="url(#arrow)" opacity="0.85"/></g>'
    )


def _node_box(node: Mapping[str, Any], at: Mapping[str, tuple[float, float]]) -> str:
    x, y = at[str(node["id"])]
    fill = FILL.get(node.get("epistemic_status") or node["kind"], "#ffffff")
    radius = NODE_H / 2 if node["kind"] == "entity" else 6
    stroke = "#444444"
    dash = ' stroke-dasharray="4 3"' if node.get("superseded") else ""
    extra = {
        k: node[k]
        for k in ("epistemic_status", "provenance_status", "origin", "experiment_kind", "modality")
        if node.get(k)
    }
    detail = ", ".join(f"{k}: {v}" for k, v in extra.items())
    title = _escape(f'{node["id"]} — {node["label"]}' + (f" [{detail}]" if detail else ""))
    label = _escape(_short(node["label"], 22))
    return (
        f"    <g><title>{title}</title>"
        f'<rect x="{x - NODE_W / 2:.1f}" y="{y - NODE_H / 2:.1f}" width="{NODE_W}" height="{NODE_H}" '
        f'rx="{radius}" fill="{fill}" stroke="{stroke}" stroke-width="1.2"{dash}/>'
        f'<text x="{x:.1f}" y="{y - 3:.1f}" text-anchor="middle" font-family="Helvetica,Arial" '
        f'font-size="11" font-weight="bold">{_escape(node["id"])}</text>'
        f'<text x="{x:.1f}" y="{y + 11:.1f}" text-anchor="middle" font-family="Helvetica,Arial" '
        f'font-size="9" fill="#333333">{label}</text></g>'
    )


def to_svg(graph: Mapping[str, Any]) -> str:
    """One self-contained SVG. Hovering a node or an edge shows the full text through the native `title`."""
    nodes = list(graph["nodes"])
    at = layout(nodes)
    edges = [e for e in graph["edges"] if str(e["source"]) in at and str(e["target"]) in at]
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {HEIGHT}" width="{WIDTH}" '
        f'height="{HEIGHT}" role="img">',
        '  <defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
        'orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="#777777"/></marker></defs>',
        f'  <rect width="{WIDTH}" height="{HEIGHT}" fill="#ffffff"/>',
        "  <g>",
    ]
    parts += [_edge_line(e, at) for e in edges]
    parts += ["  </g>", "  <g>"]
    parts += [_node_box(n, at) for n in nodes]
    parts += ["  </g>", "</svg>"]
    return "\n".join(parts) + "\n"


LEGEND = (
    ("supported", "a claim verified evidence supports"),
    ("contradicted", "verified evidence against it"),
    ("contested", "verified evidence both ways"),
    ("unresolved", "no verified relation either way"),
)


def to_html(graph: Mapping[str, Any], title: str = "Knowledge-base graph") -> str:
    """The SVG in a page that states what it is, what it is not, and what the colours mean.

    Self-contained: no stylesheet, script or font is fetched, so it opens from a file in any browser and in an
    archive years later.
    """
    swatches = "".join(
        f'<span class="k"><i style="background:{FILL[name]}"></i>{name}: {what}</span>' for name, what in LEGEND
    )
    filters = graph.get("filters") or {}
    shown = ", ".join(f"{k}={v}" for k, v in filters.items() if v not in (None, False))
    dropped = graph.get("dropped_edges") or 0
    note = (
        f"{dropped} edge(s) are not drawn: {graph.get('dropped_reason') or 'an end lies outside the selection'}."
        if dropped
        else ""
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{_escape(title)}</title>
<style>
 body {{ font-family: Helvetica, Arial, sans-serif; margin: 2rem; color: #222; background: #fff; }}
 h1 {{ font-size: 1.2rem; margin: 0 0 .3rem; }}
 p {{ max-width: 62rem; font-size: .85rem; color: #444; }}
 .k {{ display: inline-flex; align-items: center; gap: .35rem; margin: 0 .9rem .4rem 0; font-size: .78rem; }}
 .k i {{ width: .8rem; height: .8rem; border: 1px solid #555; display: inline-block; }}
 svg {{ border: 1px solid #ddd; max-width: 100%; height: auto; }}
</style></head>
<body>
<h1>{_escape(title)}</h1>
<p><strong>{len(graph["nodes"])} nodes, {len(graph["edges"])} edges.</strong> {_escape(shown)}
Records someone wrote and the links between them — <em>not</em> a causal model identified from data. A dashed
edge is an inference nobody has verified; a dashed node is superseded. Hover anything for its full text.
{_escape(note)}</p>
<p>{swatches}</p>
{to_svg(graph)}
</body></html>
"""


__all__ = ["EDGE_COLOUR", "HEIGHT", "LEGEND", "MAX_LABEL", "WIDTH", "layout", "to_html", "to_svg"]
