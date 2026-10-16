"""Tests for tools.literature.ingest: files dropped in the inbox, with the sources file and a fake Europe PMC."""

from __future__ import annotations

import json
import urllib.parse
from pathlib import Path

import pytest

from tools.literature import ingest as ing
from tools.literature.store import ExtractionError, LiteratureStore

TITLE = "Autoimmune response to C9orf72 protein in amyotrophic lateral sclerosis"
BODY = (
    "Introduction. " + "Neuroinflammation is apparent in affected tissues and T cells infiltrate the spinal cord. " * 40
)
PAPER_TXT = f"{TITLE}\nSmith J, Doe A. Nature 2025. https://doi.org/10.1038/s41586-025-09588-6\n\n{BODY}"
RECORD = {
    "pmid": "41034581",
    "pmcid": "PMC12657233",
    "doi": "10.1038/s41586-025-09588-6",
    "title": TITLE + ".",
    "pubYear": "2025",
    "authorString": "Smith J, Doe A.",
}


def _transport(records: dict[str, dict | None] | None = None):
    calls: list[str] = []

    def transport(url: str) -> str:
        calls.append(url)
        decoded = urllib.parse.unquote(url)
        for needle, record in (records or {}).items():
            if needle in decoded:
                return json.dumps({"resultList": {"result": [record] if record else []}})
        return json.dumps({"resultList": {"result": []}})

    return transport, calls


def _offline(_url: str) -> str:
    raise OSError("no network")


@pytest.fixture()
def env(tmp_path: Path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    store = LiteratureStore(tmp_path / "store", clock=lambda: "2026-09-30T12:00:00Z")
    return inbox, tmp_path / "sources.csv", store


def _run(env, transport, **kw):
    inbox, csv_path, store = env
    return ing.ingest_inbox(inbox, csv_path, store, transport, **kw)


def test_sources_row_gives_the_identifier_and_needs_no_network(env) -> None:
    inbox, csv_path, store = env
    (inbox / "paper.txt").write_text(PAPER_TXT, encoding="utf-8")
    csv_path.write_text(
        "file,identifier,title,year,alias,note\npaper.txt,10.1038/s41586-025-09588-6,My title,2025,my-alias,from the lab\n"
    )
    (result,) = _run(env, _offline)
    assert (result.outcome, result.identifier_source, result.canonical_id) == (
        "ingested",
        "sources_csv",
        "doi:10.1038/s41586-025-09588-6",
    )
    record = store.lookup("my-alias")
    assert record.title == "My title" and record.year == 2025 and "from the lab" in record.note and record.source_sha256


def test_doi_in_the_text_confirmed_by_the_title_is_inferred(env) -> None:
    inbox, _, store = env
    (inbox / "scan.txt").write_text(PAPER_TXT, encoding="utf-8")
    transport, calls = _transport({"10.1038/s41586-025-09588-6": RECORD})
    (result,) = _run(env, transport)
    assert (result.outcome, result.identifier_source) == ("ingested", "inferred_doi_title_match")
    record = store.lookup("pmid:41034581")  # all identifiers of the Europe PMC record are kept
    assert record.canonical_id == "doi:10.1038/s41586-025-09588-6" and record.alias == "smith-2025-autoimmune"
    assert "inferred" in record.note and calls


def test_pdf_text_that_splits_words_still_matches_the_title(env) -> None:
    """The real Michaelis PDF reads "C9orf 72" in its title line."""
    inbox, _, store = env
    (inbox / "real.txt").write_text(
        PAPER_TXT.replace("C9orf72", "C9orf 72", 1).replace("protein in amyotrophic", "protein in\namyotrophic", 1),
        encoding="utf-8",
    )
    transport, _ = _transport({"10.1038/s41586-025-09588-6": RECORD})
    assert [r.outcome for r in _run(env, transport)] == ["ingested"]


def test_a_very_short_title_is_never_enough(env) -> None:
    inbox, _, _ = env
    (inbox / "s.txt").write_text("Short title\nhttps://doi.org/10.1038/s41586-025-09588-6\n" + BODY, encoding="utf-8")
    transport, _ = _transport({"10.1038/s41586-025-09588-6": {**RECORD, "title": "Short title"}})
    assert [r.outcome for r in _run(env, transport)] == ["pending_identifier"]


def test_a_doi_whose_title_is_not_in_the_text_is_not_accepted(env) -> None:
    inbox, _, store = env
    (inbox / "other.txt").write_text(
        PAPER_TXT.replace(TITLE, "A completely different heading about mitochondria"), encoding="utf-8"
    )
    transport, _ = _transport({"10.1038/s41586-025-09588-6": RECORD})
    (result,) = _run(env, transport)
    assert result.outcome == "pending_identifier" and "title" in result.detail
    assert list(store.records()) == []


@pytest.mark.parametrize(
    "text, transport, expected",
    [
        ("No identifier at all in this text. " * 50, _offline, "no DOI"),
        (PAPER_TXT, _offline, "could not ask Europe PMC"),
        (PAPER_TXT, _transport({})[0], "unknown to Europe PMC"),
    ],
)
def test_without_a_confirmed_identifier_the_file_is_pending(env, text, transport, expected) -> None:
    inbox, _, store = env
    (inbox / "f.txt").write_text(text, encoding="utf-8")
    (result,) = _run(env, transport)
    assert result.outcome == "pending_identifier" and expected in result.detail and "sources file" in result.detail
    assert list(store.records()) == []


def test_a_file_is_ingested_once_and_the_users_files_are_untouched(env) -> None:
    inbox, csv_path, _ = env
    (inbox / "paper.txt").write_text(PAPER_TXT, encoding="utf-8")
    csv_path.write_text("file,identifier\npaper.txt,PMC12657233\n")
    before = {p.name: p.read_bytes() for p in inbox.iterdir()}
    assert [r.outcome for r in _run(env, _offline)] == ["ingested"]
    assert [r.outcome for r in _run(env, _offline)] == ["already_ingested"]
    assert {p.name: p.read_bytes() for p in inbox.iterdir()} == before


def test_pdf_goes_through_the_injected_extractor_and_failures_are_reported(env) -> None:
    inbox, csv_path, store = env
    (inbox / "ok.pdf").write_bytes(b"%PDF fake 1")
    (inbox / "bad.pdf").write_bytes(b"%PDF fake 2")
    (inbox / "empty.pdf").write_bytes(b"%PDF fake 3")
    csv_path.write_text("file,identifier\nok.pdf,PMC1111111\nbad.pdf,PMC2222222\nempty.pdf,PMC3333333\n")

    def extract(path: Path):
        if path.name == "bad.pdf":
            raise ExtractionError("pdftotext failed")
        return ("   " if path.name == "empty.pdf" else PAPER_TXT), "pdftotext test"

    outcomes = {r.file: r for r in _run(env, _offline, extract_pdf=extract)}
    assert outcomes["ok.pdf"].outcome == "ingested" and store.lookup("pmcid:PMC1111111").extractor == "pdftotext test"
    assert outcomes["bad.pdf"].outcome == "failed_extraction" and "pdftotext failed" in outcomes["bad.pdf"].detail
    assert (
        outcomes["empty.pdf"].outcome == "failed_extraction" and "no extractable text" in outcomes["empty.pdf"].detail
    )


def test_unsupported_hidden_and_missing_files(env) -> None:
    inbox, csv_path, _ = env
    (inbox / "notes.docx").write_bytes(b"x")
    (inbox / ".gitkeep").write_text("")
    csv_path.write_text("file,identifier\nghost.pdf,PMC4444444\n")
    outcomes = {r.file: r.outcome for r in _run(env, _offline)}
    assert outcomes == {"notes.docx": "unsupported_type", "ghost.pdf": "missing_file"}


def test_an_unrecognised_identifier_in_the_sources_file_is_pending(env) -> None:
    inbox, csv_path, store = env
    (inbox / "p.txt").write_text(PAPER_TXT, encoding="utf-8")
    csv_path.write_text("file,identifier\np.txt,not-an-id\n")
    (result,) = _run(env, _offline)
    assert result.outcome == "pending_identifier" and "not a DOI" in result.detail and list(store.records()) == []


def test_bad_sources_file_is_an_error_not_a_silent_skip(env) -> None:
    inbox, csv_path, _ = env
    csv_path.write_text("identifier,title\nPMC1,x\n")
    with pytest.raises(ValueError, match="`file` column"):
        _run(env, _offline)
    csv_path.write_text("file,identifier\n,PMC1111111\n")
    with pytest.raises(ValueError, match="without a `file`"):
        _run(env, _offline)


def test_missing_inbox_or_sources_file_means_nothing_to_do(tmp_path: Path) -> None:
    store = LiteratureStore(tmp_path / "s")
    assert ing.ingest_inbox(tmp_path / "no-inbox", tmp_path / "no.csv", store, _offline) == []
    assert ing.report([])["counts"]["ingested"] == 0


def test_records_written_before_source_sha256_existed_still_load(tmp_path: Path) -> None:
    store = LiteratureStore(tmp_path / "s")
    store.root.mkdir(parents=True)
    store.index_path.write_text(
        json.dumps(
            {
                "canonical_id": "doi:10.1/x".replace("10.1/", "10.1000/"),
                "aliases": ["doi:10.1000/x"],
                "alias": None,
                "title": "t",
                "year": None,
                "source_url": None,
                "licence": None,
                "access_date": "d",
                "status": "not_available",
                "text_path": None,
                "pdf_path": None,
                "sha256": None,
                "extractor": None,
                "run_id": None,
            }
        )
        + "\n"
    )
    assert store.find_by_source_sha256("a" * 64) is None and len(list(store.records())) == 1
