"""Read-only MCP server on the knowledge base."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from kb_factories import NOW, claim, experiment, hypothesis, relation

from knowledge_base import mcp_server as srv
from knowledge_base.store import KnowledgeBase

PACKAGE_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def kb_root(tmp_path: Path) -> Path:
    with KnowledgeBase.open(tmp_path, clock=lambda: NOW) as kb:
        kb.commit(
            [
                hypothesis(id="H-1"),
                claim(id="C-1"),
                claim(id="C-2", content="IL-10 responses are protective."),
                relation(id="L-1", type="part_of", source_id="C-1", target_id="H-1"),
                relation(id="L-2", type="supports", source_id="C-2", target_id="C-1"),
            ],
            run_id="RUN-1",
        )
    return tmp_path


def ids(answer: dict) -> list[str]:
    return [r["id"] for r in answer["records"]]


def test_tools_answer_with_compact_records_and_status(kb_root):
    got = srv.kb_get(kb_root, ["C-1"])
    assert ids(got) == ["C-1"] and got["records"][0]["epistemic_status"] == "supported"
    assert set(ids(srv.kb_neighbourhood(kb_root, ["H-1"], 1))) == {"H-1", "L-1", "C-1"}
    assert ids(srv.kb_search(kb_root, "IL-10")) == ["C-2"]


def test_errors_are_data(kb_root):
    assert "C-99" in srv.kb_get(kb_root, ["C-99"])["error"]
    assert "hops" in srv.kb_neighbourhood(kb_root, ["H-1"], 3)["error"]
    assert "limit" in srv.kb_search(kb_root, "x", 0)["error"]
    # A mistyped record type must say so, not answer as if the knowledge base held nothing like that.
    error = srv.kb_search(kb_root, "Treg", types=["Hypotheses"])["error"]
    assert "unknown record types ['Hypotheses']" in error and "Hypothesis" in error
    assert "at least one record type" in srv.kb_search(kb_root, "Treg", types=[])["error"]


def test_search_answers_with_the_evidence_unless_other_types_are_asked_for(kb_root):
    """An untyped search would otherwise answer with the system's own working records, not the evidence."""
    with KnowledgeBase.open(kb_root, clock=lambda: NOW) as kb:
        kb.commit(
            [
                experiment(
                    id=kb.allocate("experiment"),
                    hypothesis_id="H-1",
                    title="Treg depletion in a C9 mouse model",
                    tested_claims=["C-1"],
                )
            ],
            run_id="RUN-2",
        )
    assert ids(srv.kb_search(kb_root, "Treg")) == []  # the experiment is not evidence
    assert ids(srv.kb_search(kb_root, "Treg", types=["Experiment"])) == ["EXP-1"]
    assert ids(srv.kb_search(kb_root, "IL-10")) == ["C-2"]  # a claim, as before the filter


def test_the_server_sees_writes_made_after_it_started(kb_root):
    before = srv.kb_search(kb_root, "tolerance")
    with KnowledgeBase.open(kb_root, clock=lambda: NOW) as kb:
        kb.commit([claim(id=kb.allocate("claim"), content="Loss of tolerance to C9orf72.")], run_id="RUN-2")
    assert ids(before) == [] and len(ids(srv.kb_search(kb_root, "tolerance"))) == 1


def test_server_refuses_a_missing_kb(tmp_path, capsys):
    assert srv.main(["--kb", str(tmp_path / "missing"), "--check"]) == 2
    assert "cannot start" in capsys.readouterr().err


def test_real_stdio_session(kb_root):
    messages = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "kb_neighbourhood", "arguments": {"ids": ["H-1"], "hops": 1}},
        },
    ]
    proc = subprocess.Popen(
        [sys.executable, "-m", "knowledge_base.mcp_server", "--kb", str(kb_root)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=PACKAGE_ROOT,
    )
    replies: dict[int, dict] = {}
    try:
        for message in messages:
            proc.stdin.write(json.dumps(message) + "\n")
            proc.stdin.flush()
        while not {2, 3} <= replies.keys():
            line = proc.stdout.readline()
            assert line, f"server closed early; stderr: {proc.stderr.read()}"
            reply = json.loads(line)
            if "id" in reply:
                replies[reply["id"]] = reply
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)
    tools = {t["name"]: t for t in replies[2]["result"]["tools"]}
    assert sorted(tools) == ["kb_get", "kb_mechanism_graph", "kb_mechanism_path", "kb_neighbourhood", "kb_search"]
    # What an agent reads about `kb_search`: the default types, and that `types` widens the search.
    search = tools["kb_search"]
    assert '"Claim", "Entity"' in search["description"] and "types=" in search["description"]
    assert "types" in search["inputSchema"]["properties"] and "types" not in search["inputSchema"]["required"]
    assert set(ids(replies[3]["result"]["structuredContent"])) == {"H-1", "L-1", "C-1"}


def test_the_mechanism_graph_tool_returns_causal_edges_with_their_status(tmp_path):
    """The graph an agent reads is recomputed per call: the knowledge base grows during a run."""
    from kb_factories import CAUSAL, claim, entity

    from knowledge_base.mcp_server import kb_mechanism_graph
    from knowledge_base.store import KnowledgeBase

    with KnowledgeBase.open(tmp_path) as kb:
        kb.commit(
            [
                entity(id="N-1", canonical_name="C9orf72 expansion"),
                entity(id="N-2", canonical_name="motor-neuron loss"),
                claim(id="C-1", type="causal", content="The expansion drives motor-neuron loss.", **CAUSAL),
            ],
            run_id="RUN-1",
        )

    answer = kb_mechanism_graph(tmp_path)
    [edge] = answer["edges"]
    assert edge["cause_name"] == "C9orf72 expansion" and edge["effect_name"] == "motor-neuron loss"
    assert "sign" not in edge and edge["epistemic_status"] == "unresolved"
    assert answer["total"] == 1 and "not a fact" in answer["note"]
    # Filtering by entity, and a limit outside the allowed range.
    assert kb_mechanism_graph(tmp_path, entity="motor-neuron loss")["edges"] == answer["edges"]
    assert kb_mechanism_graph(tmp_path, entity="something else")["edges"] == []
    assert "error" in kb_mechanism_graph(tmp_path, limit=0)


def test_the_mechanism_path_tool_answers_with_a_connected_chain_and_its_neighbourhood(tmp_path):
    """What `kb_mechanism_graph` cannot give an agent: edges that are known to join."""
    from kb_factories import CAUSAL, claim, entity

    from knowledge_base.mcp_server import kb_mechanism_path
    from knowledge_base.store import KnowledgeBase

    def causal(identifier, cause, effect, **kw):
        fields = {**CAUSAL, "cause_ref": cause, "effect_ref": effect, "type": "causal"}
        fields.update(kw)
        return claim(id=identifier, content=f"{cause} drives {effect}", **fields)

    unchecked = {"origin": "agent", "provenance_status": "unverified", "verification_method": "none", "source_refs": []}
    with KnowledgeBase.open(tmp_path) as kb:
        kb.commit(
            [entity(id=f"N-{i}", canonical_name=f"node {i}") for i in range(1, 6)]
            + [
                causal("C-1", "N-1", "N-2"),  # verified by the factory's defaults
                causal("C-4", "N-2", "N-5"),  # verified: the chain's second step
                causal("C-2", "N-2", "N-3", **unchecked),  # unchecked, adjacent
                causal("C-3", "N-4", "N-2", **unchecked),  # unchecked, adjacent
            ],
            run_id="RUN-1",
        )

    answer = kb_mechanism_path(tmp_path)
    assert [e["claim_id"] for e in answer["backbone"]] == ["C-1", "C-4"]
    # Connected by construction: each edge's effect is the next edge's cause.
    assert [e["effect_id"] for e in answer["backbone"][:-1]] == [e["cause_id"] for e in answer["backbone"][1:]]
    assert all(e["provenance_status"] == "verified" for e in answer["backbone"])
    # The unchecked edges touching the chain are the candidates for the hypothesis step.
    assert [e["claim_id"] for e in answer["neighbourhood"]] == ["C-2", "C-3"]
    assert (answer["backbone_total"], answer["neighbourhood_total"]) == (2, 2)
    assert "Neither is a causal model identified from data" in answer["note"]
    assert "error" in kb_mechanism_path(tmp_path, limit=0)


UNCHECKED = {"origin": "agent", "provenance_status": "unverified", "verification_method": "none", "source_refs": []}


def test_the_mechanism_path_tool_reports_an_absent_chain_rather_than_inventing_one(tmp_path):
    """No proper edge, so no backbone and no neighbourhood: two unchecked assertions do not make a mechanism."""
    from kb_factories import CAUSAL, claim, entity

    from knowledge_base.mcp_server import kb_mechanism_path
    from knowledge_base.store import KnowledgeBase

    with KnowledgeBase.open(tmp_path) as kb:
        kb.commit(
            [
                entity(id="N-1"),
                entity(id="N-2", canonical_name="b"),
                entity(id="N-3", canonical_name="c"),
                entity(id="N-4", canonical_name="d"),
                claim(
                    id="C-1",
                    type="causal",
                    content="a drives b",
                    **{**CAUSAL, "cause_ref": "N-1", "effect_ref": "N-2", **UNCHECKED},
                ),
                claim(
                    id="C-2",
                    type="causal",
                    content="c drives d",
                    **{**CAUSAL, "cause_ref": "N-3", "effect_ref": "N-4", **UNCHECKED},
                ),
            ],
            run_id="RUN-1",
        )

    answer = kb_mechanism_path(tmp_path)
    assert answer["backbone"] == [] and answer["neighbourhood"] == []
    assert answer["backbone_total"] == 0


def test_no_served_tool_reveals_who_wrote_a_hypothesis_or_how_sure_they_were(kb_root):
    """`review` is blinded (`workflows/phases/src/review.js`), and the orchestrator stripped the author fields
    from the slice it pushes into a prompt; the tools an agent pulls must strip them too, or a reviewer can
    read `agent_id` and `author_confidence` through `kb_get` or `kb_neighbourhood`."""
    for answer in (
        srv.kb_get(kb_root, ["H-1"]),
        srv.kb_neighbourhood(kb_root, ["H-1"], 1),
        srv.kb_search(kb_root, "s", types=["Hypothesis"]),
    ):
        for record in answer.get("records", []):
            assert "agent_id" not in record and "author_confidence" not in record, record
    # The record is still served, and still identified: only the two author fields are withheld.
    served = srv.kb_get(kb_root, ["H-1"])["records"][0]
    assert served["id"] == "H-1" and served["statement"]
