"""Graphs built from the knowledge base: what enters, what is dropped, and how it renders."""

from __future__ import annotations

import json

from kb_factories import CAUSAL, claim, decision, entity, experiment, hypothesis, ignorance, relation

from tools.graph.build import build
from tools.graph.cli import main
from tools.graph.render import to_dot, to_mermaid


def kb(*records):
    return {r.id: r for r in records}


BASE = kb(
    entity(id="N-1", canonical_name="C9orf72 expansion"),
    entity(id="N-2", canonical_name="motor-neuron loss"),
    claim(id="C-1", type="causal", content="The expansion drives motor-neuron loss.", **CAUSAL),
    claim(id="C-2", content="DPRs are detected in C9-ALS spinal cord."),
    hypothesis(id="H-1"),
    ignorance(id="I-1"),
    experiment(id="EXP-1"),
    decision(id="D-1"),
    relation(
        id="L-1",
        type="part_of",
        source_id="C-1",
        target_id="H-1",
        justification_method="agent_inference",
        provenance_status="unverified",
        source_refs=[],
    ),
    relation(id="L-2", type="supports", source_id="C-2", target_id="C-1"),
)


def test_every_record_kind_becomes_a_node_and_relations_become_edges():
    graph = build(BASE)
    kinds = {n["kind"] for n in graph["nodes"]}
    assert kinds == {"entity", "claim", "hypothesis", "ignorance", "experiment", "decision"}
    assert {(e["source"], e["target"], e["kind"]) for e in graph["edges"]} >= {
        ("C-1", "H-1", "part_of"),
        ("C-2", "C-1", "supports"),
        ("N-1", "C-1", "cause"),
        ("C-1", "N-2", "effect"),  # a causal claim sits between its entities
        ("EXP-1", "H-1", "about"),
        ("D-1", "EXP-1", "buys"),
    }  # links no Relation record carries


def test_a_claim_node_carries_its_computed_status_and_its_provenance():
    [node] = [n for n in build(BASE)["nodes"] if n["id"] == "C-1"]
    assert node["epistemic_status"] == "supported"  # a verified supports relation from C-2
    assert node["provenance_status"] == "verified" and node["type"] == "causal"


def test_filtering_by_run_keeps_the_run_and_counts_the_edges_it_cuts():
    records = kb(
        *BASE.values(),
        claim(id="C-9", content="from another run", run_id="RUN-2"),
        relation(id="L-9", type="supports", source_id="C-9", target_id="C-1", run_id="RUN-2"),
    )
    whole = build(records)
    one_run = build(records, run_id="RUN-2")
    assert {n["id"] for n in one_run["nodes"]} == {"C-9"}
    # The edge into RUN-1 is dropped, and the reader is told why rather than left to notice a missing arrow.
    assert one_run["dropped_edges"] >= 1 and "earlier run" in one_run["dropped_reason"]
    assert whole["dropped_edges"] == 0 and whole["dropped_reason"] == ""


def test_a_focus_keeps_a_neighbourhood():
    graph = build(BASE, focus=["H-1"], hops=1)
    ids = {n["id"] for n in graph["nodes"]}
    assert "H-1" in ids and "C-1" in ids  # one hop crosses the part_of relation
    assert "N-2" not in ids  # two hops away


def test_a_superseded_record_is_hidden_unless_asked_for():
    records = kb(
        *BASE.values(),
        claim(id="C-3", content="a better wording of C-2"),
        relation(
            id="L-3",
            type="supersedes",
            source_id="C-3",
            target_id="C-2",
            justification_method="agent_inference",
            provenance_status="unverified",
            source_refs=[],
        ),
    )
    assert "C-2" not in {n["id"] for n in build(records)["nodes"]}
    shown = [n for n in build(records, include_superseded=True)["nodes"] if n["id"] == "C-2"]
    assert shown and shown[0]["superseded"] is True


def test_dot_colours_by_status_and_escapes_quotes():
    records = kb(entity(id="N-1", canonical_name='an entity with "quotes"'))
    dot = to_dot(build(records))
    assert '\\"quotes\\"' in dot and "digraph" in dot
    full = to_dot(build(BASE))
    assert '"C-1" [label="C-1' in full and "#cfe9cf" in full  # supported claim
    assert 'color="#b23b3b"' not in full or "contradicts" in full


def test_mermaid_marks_an_unverified_relation_with_a_dashed_arrow():
    text = to_mermaid(build(BASE))
    assert "flowchart LR" in text
    assert "C_1 -.->|part_of| H_1" in text  # unverified inference
    assert "C_2 -->|supports| C_1" in text  # verified


def test_the_cli_writes_a_file_and_refuses_to_overwrite_it(tmp_path, capsys):
    from knowledge_base.store import KnowledgeBase

    root = tmp_path / "kb"
    with KnowledgeBase.open(root) as store:
        store.commit([entity(id="N-1", canonical_name="C9orf72 expansion")], run_id="RUN-1")
    out = tmp_path / "out" / "graph.json"
    assert main(["--kb", str(root), "--format", "json", "--out", str(out)]) == 0
    assert json.loads(out.read_text())["nodes"][0]["id"] == "N-1"
    assert main(["--kb", str(root), "--format", "json", "--out", str(out)]) == 2
    assert "refusing to overwrite" in capsys.readouterr().err


def test_the_cli_fails_cleanly_on_a_missing_knowledge_base(tmp_path, capsys):
    assert main(["--kb", str(tmp_path / "absent")]) == 2
    assert "cannot read the knowledge base" in capsys.readouterr().err


# --- the dependency-free picture -----------------------------------------------------------------------------


def test_the_svg_is_self_contained_and_has_one_box_per_node():
    from tools.graph.svg import HEIGHT, WIDTH, to_svg

    svg = to_svg(build(BASE))
    nodes = len(build(BASE)["nodes"])
    assert svg.count("<rect") == nodes + 1  # one per node, plus the white background
    assert f'viewBox="0 0 {WIDTH} {HEIGHT}"' in svg
    # Nothing is fetched: the only http in the file is the SVG namespace.
    assert svg.count("http") == 1 and "xmlns" in svg
    assert "<script" not in svg  # a picture, not a program


def test_the_layout_is_deterministic_and_inside_the_canvas():
    from tools.graph.svg import HEIGHT, WIDTH, layout

    graph = build(BASE)
    first, second = layout(graph["nodes"]), layout(list(reversed(graph["nodes"])))
    assert first == second  # a figure in a report must not move between runs
    assert all(0 < x < WIDTH and 0 < y < HEIGHT for x, y in first.values())


def test_an_unverified_edge_is_drawn_differently_from_a_verified_one():
    from tools.graph.svg import to_svg

    svg = to_svg(build(BASE))
    # C-1 -part_of-> H-1 is an agent inference; C-2 -supports-> C-1 is verified.
    dashed = [line for line in svg.splitlines() if "stroke-dasharray" in line and "part_of" in line]
    assert dashed and "unverified inference" in dashed[0]
    assert not any("stroke-dasharray" in line and "supports" in line for line in svg.splitlines())


def test_hovering_shows_the_full_text_a_label_had_to_cut():
    from tools.graph.svg import to_svg

    long_claim = claim(id="C-7", content="A claim far longer than any box label can hold, " * 3)
    svg = to_svg(build(kb(long_claim)))
    assert "far longer than any box label" in svg  # in the title
    assert "…" in svg  # and cut in the drawn label


def test_the_html_page_states_what_the_picture_is_not_and_explains_the_colours():
    from tools.graph.svg import to_html

    page = to_html(build(BASE), title="a run")
    assert "not</em> a causal model identified from data" in page
    assert "supported" in page and "contested" in page  # the legend
    assert page.startswith("<!doctype html>") and "<svg" in page
    assert page.count("http") == 1  # self-contained, nothing fetched


def test_the_html_says_how_many_edges_are_not_drawn():
    from tools.graph.svg import to_html

    records = kb(
        *BASE.values(),
        claim(id="C-9", content="from another run", run_id="RUN-2"),
        relation(id="L-9", type="supports", source_id="C-9", target_id="C-1", run_id="RUN-2"),
    )
    page = to_html(build(records, run_id="RUN-2"))
    assert "not drawn" in page and "earlier run" in page


def test_the_cli_writes_every_format(tmp_path):
    from knowledge_base.store import KnowledgeBase

    root = tmp_path / "kb"
    with KnowledgeBase.open(root) as store:
        store.commit([entity(id="N-1", canonical_name="C9orf72 expansion")], run_id="RUN-1")
    for fmt, marker in (
        ("svg", "<svg"),
        ("html", "<!doctype html>"),
        ("dot", "digraph"),
        ("mermaid", "flowchart"),
        ("json", '"nodes"'),
    ):
        out = tmp_path / f"graph.{fmt}"
        assert main(["--kb", str(root), "--format", fmt, "--out", str(out)]) == 0, fmt
        assert marker in out.read_text(), fmt


def test_mermaid_colours_a_claim_by_its_epistemic_status_and_a_hypothesis_by_its_kind():
    graph = build(BASE)
    for node in graph["nodes"]:
        if node["id"] == "C-1":
            node["epistemic_status"] = "contradicted"
    text = to_mermaid(graph)
    assert "C_1[" in text and ":::contradicted" in text
    assert "classDef contradicted fill:#f2c7c7" in text
    assert "H_1{{" in text and ":::hypothesis" in text and "classDef hypothesis fill:#ede3f7" in text
