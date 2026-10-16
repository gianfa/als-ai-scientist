"""Tests for tools.literature.store: identifier canonicalisation and the paper store (no network)."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from tools.literature import store as lit

FULL = "Some full text of a paper. " * 200  # long enough to count as full text
DOI = "10.1038/s41586-025-09588-6"


@pytest.mark.parametrize(
    "raw, expected",
    [
        (DOI, f"doi:{DOI}"),
        (f"doi:{DOI}", f"doi:{DOI}"),
        (f"DOI:{DOI.upper()}", f"doi:{DOI}"),
        (f"https://doi.org/{DOI}", f"doi:{DOI}"),
        (f"http://dx.doi.org/{DOI}/", f"doi:{DOI}"),
        ("PMID:41034581", "pmid:41034581"),
        ("pmid 41034581", "pmid:41034581"),
        ("https://pubmed.ncbi.nlm.nih.gov/41034581/", "pmid:41034581"),
        ("PMC12586167", "pmcid:PMC12586167"),
        ("pmcid:pmc12586167", "pmcid:PMC12586167"),
        ("https://www.ncbi.nlm.nih.gov/pmc/articles/PMC12586167/", "pmcid:PMC12586167"),
        ("https://www.nature.com/articles/s41586-025-09588-6", f"doi:{DOI}"),
        ("http://nature.com/articles/s41586-025-09588-6/?error=cookies", f"doi:{DOI}"),
        ("https://www.nature.com/articles/nm.4490", "doi:10.1038/nm.4490"),
    ],
)
def test_canonical_id_normalises_the_common_spellings(raw: str, expected: str) -> None:
    assert lit.canonical_id(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "not an id", "https://example.org/paper", None])
def test_canonical_id_refuses_what_it_cannot_recognise(raw) -> None:
    assert lit.canonical_id(raw) is None


def _store(tmp_path: Path) -> lit.LiteratureStore:
    return lit.LiteratureStore(tmp_path / "literature", clock=lambda: "2026-09-30T12:00:00Z")


def _add(store: lit.LiteratureStore, **over):
    args = dict(
        identifiers=[f"doi:{DOI}", "PMID:41034581"],
        title="Autoimmune response to C9orf72 protein in ALS",
        year=2025,
        text=FULL,
        source_url=f"https://doi.org/{DOI}",
        licence="CC BY-NC-ND 4.0",
        extractor="test-extractor 1.0",
        run_id="e2e-test",
        alias="michaelis-2025-c9orf72-autoimmunity",
    )
    args.update(over)
    return store.add(**args)


def rec_json(record) -> str:
    """The record as the index holds it, for tests that hand-write an index line."""
    return record.to_json()


def test_add_then_lookup_by_any_alias(tmp_path: Path) -> None:
    store = _store(tmp_path)
    rec = _add(store)
    assert rec.status == "full_text"
    assert rec.canonical_id == f"doi:{DOI}"
    for raw in (DOI, "pmid:41034581", f"https://doi.org/{DOI}", "michaelis-2025-c9orf72-autoimmunity"):
        found = store.lookup(raw)
        assert found is not None and found.canonical_id == rec.canonical_id, raw
    assert not Path(found.text_path).is_absolute()  # relative to the store root
    assert store.resolve(found.text_path).read_text(encoding="utf-8") == FULL
    assert found.sha256 and found.access_date == "2026-09-30T12:00:00Z" and found.licence == "CC BY-NC-ND 4.0"
    assert found.code_available == "unknown" and found.data_available == "unknown"


def test_lookup_of_an_unknown_paper_is_none(tmp_path: Path) -> None:
    assert _store(tmp_path).lookup("10.1000/never-added") is None
    assert _store(tmp_path).lookup("garbage") is None


def test_index_is_append_only_and_the_newest_record_wins(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _add(store)
    _add(store, text=FULL + " Corrected version.")
    lines = (store.root / "index.jsonl").read_text().splitlines()
    assert len(lines) == 2  # nothing rewritten
    assert json.loads(lines[0])["sha256"] != json.loads(lines[1])["sha256"]
    assert store.lookup(DOI).sha256 == json.loads(lines[1])["sha256"]
    assert len(list(store.records())) == 1  # one paper, latest record


def test_a_tampered_text_file_is_treated_as_absent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    rec = _add(store)
    store.resolve(rec.text_path).write_text("changed on disk", encoding="utf-8")
    assert store.lookup(DOI) is None  # sha256 mismatch: never trust it silently
    assert store.lookup(DOI, verify=False) is not None  # but the record is still visible for audit


def test_thin_text_is_abstract_only_never_full_text(tmp_path: Path) -> None:
    rec = _add(_store(tmp_path), text="Only an abstract.")
    assert rec.status == "abstract_only"


def test_a_record_needs_at_least_one_recognisable_identifier(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="identifier"):
        _add(_store(tmp_path), identifiers=["nonsense"])


def test_records_without_text_are_kept_as_not_available(tmp_path: Path) -> None:
    store = _store(tmp_path)
    rec = store.add_unavailable(
        identifiers=["10.1000/paywalled"], title="Paywalled paper", reason="paywall", run_id="r1"
    )
    assert rec.status == "not_available" and rec.text_path is None
    assert store.lookup("10.1000/paywalled").status == "not_available"


def test_index_markdown_is_rendered_from_the_index(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _add(store)
    store.add_unavailable(identifiers=["10.1000/paywalled"], title="Paywalled paper", reason="paywall", run_id="r1")
    md = store.render_index_md()
    assert (store.root / "INDEX.md").read_text() == md
    assert "michaelis-2025-c9orf72-autoimmunity" in md and "doi:10.1000/paywalled" in md
    assert "full_text" in md and "not_available" in md
    assert "generated" in md.lower()  # says it must not be edited by hand


def test_concurrent_adds_do_not_corrupt_the_index(tmp_path: Path) -> None:
    store = _store(tmp_path)
    errors: list[Exception] = []

    def worker(i: int) -> None:
        try:
            for j in range(5):
                store.add(
                    identifiers=[f"10.1000/p{i}-{j}"],
                    title=f"t{i}-{j}",
                    year=2026,
                    text=FULL + f"{i}{j}",
                    source_url="u",
                    licence="CC BY 4.0",
                    extractor="x",
                    run_id="r",
                )
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    lines = (store.root / "index.jsonl").read_text().splitlines()
    assert len(lines) == 20 and all(json.loads(line) for line in lines)


def test_snapshot_lists_every_paper_with_its_hash(tmp_path: Path) -> None:
    store = _store(tmp_path)
    rec = _add(store)
    snap = store.snapshot()
    assert snap == [
        {
            "canonical_id": rec.canonical_id,
            "alias": rec.alias,
            "status": "full_text",
            "text_path": rec.text_path,
            "sha256": rec.sha256,
            "licence": "CC BY-NC-ND 4.0",
        }
    ]


def test_pdf_extraction_uses_the_injected_runner_and_records_the_version(tmp_path: Path) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-fake")
    calls: list[list[str]] = []

    def runner(cmd, **kwargs):
        calls.append(list(cmd))

        class R:  # minimal CompletedProcess stand-in
            returncode = 0
            stdout = FULL
            stderr = "pdftotext version 22.12.0"

        return R()

    text, extractor = lit.extract_pdf_text(pdf, runner=runner)
    assert text == FULL and "pdftotext" in extractor and "22.12.0" in extractor
    assert calls[0][0] == "pdftotext" and str(pdf) in calls[0]


def test_pdf_extraction_failure_is_an_error_not_an_empty_text(tmp_path: Path) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"x")

    def runner(cmd, **kwargs):
        class R:
            returncode = 1
            stdout = ""
            stderr = "Syntax Error: Couldn't read xref table"

        return R()

    with pytest.raises(lit.ExtractionError, match="xref"):
        lit.extract_pdf_text(pdf, runner=runner)


# ---- stored paths are relative to the store root -----------------------------------------------------


def test_a_stored_path_is_relative_so_the_store_survives_being_moved(tmp_path: Path) -> None:
    """Absolute paths would orphan every text already on disk as soon as the store is moved."""
    store = _store(tmp_path)
    rec = _add(store, extras="Table 1. Counts.", refs="[1] A paper.")
    for stored in (rec.text_path, rec.extras_path, rec.refs_path):
        assert stored and not Path(stored).is_absolute(), stored
    moved = tmp_path / "elsewhere" / "store"
    moved.parent.mkdir()
    store.root.rename(moved)
    after = lit.LiteratureStore(moved)
    found = after.lookup(DOI)
    assert found is not None, "a moved store must still resolve its own texts"
    assert after.resolve(found.text_path).read_text(encoding="utf-8") == FULL
    assert after.companion(found, "extras") == "Table 1. Counts."


def test_a_legacy_absolute_path_is_still_honoured(tmp_path: Path) -> None:
    store = _store(tmp_path)
    rec = _add(store)
    absolute = str(store.resolve(rec.text_path))
    (store.root / "index.jsonl").write_text(
        json.dumps({**json.loads(rec_json(rec)), "text_path": absolute}) + "\n", encoding="utf-8"
    )
    assert store.lookup(DOI) is not None
    assert store.resolve(absolute) == Path(absolute)


def test_relink_reports_without_writing_and_then_repairs(tmp_path: Path) -> None:
    store = _store(tmp_path)
    rec = _add(store)
    real = store.resolve(rec.text_path)
    stale = f"/gone/{tmp_path.name}/../literature/store/{rec.text_path}"  # the shape the flattening left
    (store.root / "index.jsonl").write_text(
        json.dumps({**json.loads(rec_json(rec)), "text_path": stale}) + "\n", encoding="utf-8"
    )
    assert store.lookup(DOI) is None  # the text is on disk but unreachable

    report = store.relink_paths()
    assert report["dry_run"] is True and list(report["relinked"]) == [rec.canonical_id]
    assert len((store.root / "index.jsonl").read_text().splitlines()) == 1  # nothing written
    assert store.lookup(DOI) is None

    applied = store.relink_paths(dry_run=False)
    assert applied["relinked"][rec.canonical_id]["text_path"] == rec.text_path
    lines = (store.root / "index.jsonl").read_text().splitlines()
    assert len(lines) == 2 and json.loads(lines[0])["text_path"] == stale  # the old record is preserved
    found = store.lookup(DOI)
    assert found is not None and found.text_path == rec.text_path
    assert store.resolve(found.text_path) == real


def test_relink_refuses_a_file_that_does_not_match_the_recorded_sha256(tmp_path: Path) -> None:
    store = _store(tmp_path)
    rec = _add(store)
    store.resolve(rec.text_path).write_text("a different paper entirely", encoding="utf-8")
    stale = f"/gone/{rec.text_path}"
    (store.root / "index.jsonl").write_text(
        json.dumps({**json.loads(rec_json(rec)), "text_path": stale}) + "\n", encoding="utf-8"
    )
    report = store.relink_paths(dry_run=False)
    assert report["relinked"] == {} and len(report["mismatched"]) == 1
    assert report["mismatched"][0]["paper"] == rec.canonical_id
    assert len((store.root / "index.jsonl").read_text().splitlines()) == 1  # untouched


def test_a_pdf_inside_the_store_is_relativised_and_one_outside_is_not(tmp_path: Path) -> None:
    store = _store(tmp_path)
    inside = store.root / "pdf" / "paper.pdf"
    inside.parent.mkdir(parents=True)
    inside.write_bytes(b"%PDF-fake")
    outside = tmp_path / "inbox" / "paper.pdf"
    outside.parent.mkdir()
    outside.write_bytes(b"%PDF-fake")
    assert _add(store, pdf_path=str(inside)).pdf_path == "pdf/paper.pdf"
    kept = _add(store, identifiers=["10.1000/other"], pdf_path=str(outside)).pdf_path
    assert kept == str(outside)  # the store does not own the inbox
    assert _add(store, identifiers=["10.1000/third"], pdf_path=None).pdf_path is None


def test_integrity_names_a_missing_text_and_a_changed_one(tmp_path: Path) -> None:
    store = _store(tmp_path)
    good = _add(store)
    gone = _add(store, identifiers=["10.1000/gone"], text=FULL + " second")
    changed = _add(store, identifiers=["10.1000/changed"], text=FULL + " third")
    assert store.integrity()["sound"] is True

    store.resolve(gone.text_path).unlink()
    store.resolve(changed.text_path).write_text("rewritten by something else", encoding="utf-8")
    report = store.integrity()
    assert report["sound"] is False and report["papers"] == 3 and report["readable"] == 1
    assert [r["paper"] for r in report["missing"]] == [gone.canonical_id]
    assert [r["paper"] for r in report["mismatched"]] == [changed.canonical_id]
    assert good.canonical_id not in [r["paper"] for r in report["missing"] + report["mismatched"]]


def test_integrity_counts_a_paper_with_no_text_apart_from_a_broken_one(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _add(store)
    store.add_unavailable(identifiers=["10.1000/paywalled"], title="Paywalled", reason="paywall", run_id="e2e-test")
    report = store.integrity()
    assert report["sound"] is True and report["readable"] == 1
    assert report["no_text_recorded"] == ["doi:10.1000/paywalled"]
