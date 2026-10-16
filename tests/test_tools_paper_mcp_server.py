"""`tools.paper.mcp_server`: it starts only on a real extraction, and refuses rather than raises."""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.paper import mcp_server
from tools.paper.document import MAX_PASSAGE_LINES, PaperDocument

MARKDOWN = "# Paper\n\nNeuroinflammation is apparent in the affected tissues.\n"


@pytest.fixture
def extraction(tmp_path) -> Path:
    path = tmp_path / "paper.md"
    path.write_text(MARKDOWN, encoding="utf-8")
    return path


def test_check_succeeds_on_a_real_extraction(extraction, capsys):
    assert mcp_server.main(["--paper", str(extraction), "--check"]) == 0
    assert "paper MCP server: ok" in capsys.readouterr().err


@pytest.mark.parametrize(
    "make, expected",
    [
        (lambda d: d / "absent.md", "does not exist"),
        # A file without headings is not the extraction; starting on it would serve something else as the paper.
        (lambda d: _write(d / "wrong.md", "no headings here, just prose\n"), "no headings found"),
    ],
)
def test_it_refuses_to_start_on_the_wrong_file(tmp_path, capsys, make, expected):
    assert mcp_server.main(["--paper", str(make(tmp_path)), "--check"]) == 2
    assert expected in capsys.readouterr().err


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_the_tool_descriptions_tell_the_agent_the_rules(extraction):
    """An agent reads only the description, so the two rules that keep the evidence chain must be in it."""
    assert "check_quote" in mcp_server.PASSAGE_DESCRIPTION
    assert "outside_main_text" in mcp_server.PASSAGE_DESCRIPTION
    assert str(MAX_PASSAGE_LINES) in mcp_server.PASSAGE_DESCRIPTION
    assert "sha256" in mcp_server.NAVIGATE_DESCRIPTION


def test_the_served_answers_are_the_document_s(extraction):
    """The server adds no logic of its own: it is the document, over stdio."""
    document = PaperDocument(extraction)
    assert document.navigate()["sha256"] == document.sha256
    assert document.passage(heading="Paper")["text"] == "Neuroinflammation is apparent in the affected tissues."
    assert document.passage(start=1, end=10_000)["status"] == "refused"
