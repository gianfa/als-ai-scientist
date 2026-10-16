"""Tests for tools.literature.fetch with a fake transport (no network)."""

from __future__ import annotations

import email.message
import email.utils
import hashlib
import json
import random
import urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import pytest

from tools.literature import fetch as lf
from tools.literature.store import ExtractionError, LiteratureStore

PARA = "The paragraph carries a finding about C9orf72 that is long enough to count. " * 40
JATS = f"""<?xml version="1.0"?>
<article><front><article-meta><title-group><article-title>A paper on C9orf72</article-title></title-group>
<abstract><p>Abstract sentence.</p></abstract></article-meta></front>
<body><sec><title>Results</title><p>{PARA}<xref ref-type="bibr" rid="B1">[1]</xref> after<fig><label>Fig 1</label></fig>.</p>
<table-wrap><p>dropped table text</p></table-wrap></sec></body></article>"""


def _record(pmcid: str | None = "PMC1001", open_access: str = "Y", doi: str = "10.1000/x", pmid: str = "11100") -> dict:
    return {
        "pmid": pmid,
        "pmcid": pmcid,
        "doi": doi,
        "title": "The C9orf72 Paper",
        "pubYear": "2025",
        "authorString": "Smith J, Doe A.",
        "isOpenAccess": open_access,
        "license": "cc by",
    }


def _transport(
    records: dict[str, dict | None], fulltext: dict[str, str] | None = None
) -> tuple[Callable[[str], str], list[str]]:
    calls: list[str] = []

    def transport(url: str) -> str:
        calls.append(url)
        if "/search?" in url:
            for needle, record in records.items():
                if needle in urllib.parse.unquote(url):
                    return json.dumps({"resultList": {"result": [record] if record else []}})
            return json.dumps({"resultList": {"result": []}})
        for pmcid, body in (fulltext or {}).items():
            if f"/{pmcid}/fullTextXML" in url:
                return body
        raise OSError("404")

    return transport, calls


def _store(tmp_path: Path) -> LiteratureStore:
    return LiteratureStore(tmp_path / "lit", clock=lambda: "2026-09-30T12:00:00Z")


def test_jats_to_text_keeps_paragraphs_and_drops_markers_tables_figures() -> None:
    text = lf.jats_to_text(JATS)
    assert "A paper on C9orf72" in text and "Abstract sentence." in text and "Results" in text
    assert "[1]" not in text and "Fig 1" not in text and "dropped table text" not in text
    assert "after." in text


def test_jats_parts_keeps_tables_captions_and_references_apart() -> None:
    parts = lf.jats_parts(JATS)
    assert "dropped table text" in parts.extras and "dropped table text" not in parts.main
    assert "[1]" not in parts.main


def test_jats_parts_keeps_figure_cross_references_in_the_text_and_drops_bibliography_markers() -> None:
    xml = (
        '<article><body><p>Levels rose <xref ref-type="fig" rid="f2">Fig. 2b</xref> in patients '
        '<xref ref-type="bibr" rid="r1">[1]</xref> overall.</p></body>'
        "<back><ref-list><ref><mixed-citation>Doe 2020 A cited paper</mixed-citation></ref></ref-list></back></article>"
    )
    parts = lf.jats_parts(xml)
    assert "Fig. 2b" in parts.main and "[1]" not in parts.main
    assert "A cited paper" in parts.refs and "A cited paper" not in parts.main


def test_jats_to_text_rejects_invalid_xml() -> None:
    with pytest.raises(lf.FetchError):
        lf.jats_to_text("<article><p>unclosed")


def test_fetch_stores_open_access_paper_and_records_alias(tmp_path: Path) -> None:
    store = _store(tmp_path)
    transport, _ = _transport({"DOI": _record()}, {"PMC1001": JATS})
    (result,) = lf.fetch_reading_list(["10.1000/x"], store, transport, run_id="r1")
    assert result.outcome == "fetched"
    rec = store.lookup("doi:10.1000/x")
    assert rec is not None and rec.status == "full_text" and rec.alias == "smith-2025-c9orf72"
    assert rec.extractor == lf.EXTRACTOR and rec.licence == "cc by"
    assert store.lookup("pmid:11100") == rec and store.lookup("smith-2025-c9orf72") == rec
    assert "dropped table text" in (store.companion(rec, "extras") or "")


def test_cached_paper_uses_no_network_and_no_cap(tmp_path: Path) -> None:
    store = _store(tmp_path)
    transport, calls = _transport({"DOI": _record()}, {"PMC1001": JATS})
    lf.fetch_reading_list(["10.1000/x"], store, transport)
    calls.clear()
    (result,) = lf.fetch_reading_list(["10.1000/x"], store, transport, max_new=0)
    assert result.outcome == "cached" and calls == []


def test_paywalled_is_recorded_and_not_retried(tmp_path: Path) -> None:
    store = _store(tmp_path)
    transport, calls = _transport({"DOI": _record(pmcid=None, open_access="N")})
    (first,) = lf.fetch_reading_list(["10.1000/x"], store, transport)
    assert first.outcome == "paywalled" and store.lookup("doi:10.1000/x").status == "not_available"
    calls.clear()
    (second,) = lf.fetch_reading_list(["10.1000/x"], store, transport)
    assert second.outcome == "known_unavailable" and calls == []
    (third,) = lf.fetch_reading_list(["10.1000/x"], store, transport, retry_unavailable=True)
    assert third.outcome == "paywalled" and calls


def test_unrecognised_and_unknown_identifiers_are_unresolvable(tmp_path: Path) -> None:
    store = _store(tmp_path)
    transport, _ = _transport({})
    results = lf.fetch_reading_list(["not an id", "10.9/unknown"], store, transport)
    assert [r.outcome for r in results] == ["unresolvable", "unresolvable"]
    assert list(store.records()) == []  # nothing invented or recorded


def test_cap_counts_only_new_downloads_and_leaves_skipped_unrecorded(tmp_path: Path) -> None:
    store = _store(tmp_path)
    records = {
        f"10.1000/p{i}": _record(pmcid=f"PMC{1000 + i}", doi=f"10.1000/p{i}", pmid=str(10000 + i)) for i in range(3)
    }
    transport, _ = _transport(records, {f"PMC{1000 + i}": JATS for i in range(3)})
    entries = ["10.1000/p0", "10.1000/p1", "10.1000/p2"]
    outcomes = [r.outcome for r in lf.fetch_reading_list(entries, store, transport, max_new=2)]
    assert outcomes == ["fetched", "fetched", "skipped_over_cap"]
    assert store.lookup("doi:10.1000/p2") is None
    outcomes = [r.outcome for r in lf.fetch_reading_list(entries, store, transport, max_new=2)]
    assert outcomes == ["cached", "cached", "fetched"]


def test_download_failure_records_nothing(tmp_path: Path) -> None:
    store = _store(tmp_path)
    transport, _ = _transport({"DOI": _record()}, {})  # search works, full text 404s
    (result,) = lf.fetch_reading_list(["10.1000/x"], store, transport)
    assert result.outcome == "failed" and store.lookup("doi:10.1000/x") is None


def test_malformed_search_response_is_a_failure_not_a_crash(tmp_path: Path) -> None:
    store = _store(tmp_path)
    (result,) = lf.fetch_reading_list(["10.1000/x"], store, lambda url: "<html>rate limited</html>")
    assert result.outcome == "failed"


def test_duplicates_yield_one_result_and_negative_cap_is_refused(tmp_path: Path) -> None:
    store = _store(tmp_path)
    transport, _ = _transport({"DOI": _record()}, {"PMC1001": JATS})
    results = lf.fetch_reading_list(
        ["10.1000/x", "doi:10.1000/X", {"identifier": "https://doi.org/10.1000/x"}], store, transport
    )
    assert len(results) == 1
    with pytest.raises(ValueError):
        lf.fetch_reading_list([], store, transport, max_new=-1)


def test_report_counts_every_outcome_and_reading_list_formats(tmp_path: Path) -> None:
    rep = lf.report([lf.EntryResult("a", "doi:a", "fetched"), lf.EntryResult("b", None, "unresolvable")])
    assert rep["counts"]["fetched"] == 1 and rep["counts"]["unresolvable"] == 1 and rep["counts"]["cached"] == 0
    listing = tmp_path / "list.json"
    listing.write_text(json.dumps({"reading_list": ["10.1000/x"]}))
    assert lf.load_reading_list(listing) == ["10.1000/x"]
    listing.write_text(json.dumps({"other": 1}))
    with pytest.raises(ValueError):
        lf.load_reading_list(listing)


def test_make_alias_needs_author_year_and_title() -> None:
    assert (
        lf.make_alias({"authorString": "Smith J", "pubYear": "2025", "title": "The C9orf72 x"}) == "smith-2025-c9orf72"
    )
    assert lf.make_alias({"pubYear": "2025", "title": "T"}) is None


def test_every_tracked_reading_list_is_loadable_and_its_identifiers_are_recognised() -> None:
    """The lists in literature/reading-lists/ are inputs of a reproducible fetch: a typo must not go unnoticed."""
    from tools.literature.store import REPO_ROOT, canonical_id

    lists = sorted((REPO_ROOT / "literature" / "reading-lists").glob("*.json"))
    assert lists, "no reading list found"
    for path in lists:
        entries = lf.load_reading_list(path)
        assert entries, f"{path.name} is empty"
        for entry in entries:
            identifier = entry if isinstance(entry, str) else entry.get("identifier")
            assert canonical_id(identifier) is not None, f"{path.name}: unrecognised identifier {identifier!r}"


# ---- OpenAlex fallback --------------------------------------------------------------------------------

PDF = b"%PDF-1.7 fake body"
PDF_TEXT = "Text extracted from the published PDF about C9orf72. " * 60


def _location(url: str, version: str = "publishedVersion", licence: str | None = "cc-by") -> dict:
    return {"pdf_url": url, "version": version, "license": licence, "landing_page_url": "https://pub.example/x"}


def _work(*locations: dict) -> dict:
    return {
        "id": "https://openalex.org/W1",
        "updated_date": "2026-09-01T00:00:00",
        "best_oa_location": locations[0] if locations else None,
        "locations": list(locations),
    }


def _with_openalex(
    base: Callable[[str], str], work: dict | None, root: str | None = '{"version": "0.1"}'
) -> tuple[Callable[[str], str], list[str]]:
    """Wrap a Europe PMC fake: OpenAlex URLs answer with `work` (None = 404) and `root` (None = unreachable)."""
    calls: list[str] = []

    def transport(url: str) -> str:
        if url.startswith(lf.OPENALEX):
            calls.append(url)
            if "/works/" in url:
                if work is None:
                    raise urllib.error.HTTPError(url, 404, "Not Found", hdrs=None, fp=None)  # type: ignore[arg-type]
                return json.dumps(work)
            if root is None:
                raise OSError("unreachable")
            return root
        return base(url)

    return transport, calls


def _pdfs(bodies: dict[str, bytes | Exception]) -> tuple[Callable[[str], bytes], list[str]]:
    calls: list[str] = []

    def pdf_transport(url: str) -> bytes:
        calls.append(url)
        body = bodies[url]
        if isinstance(body, Exception):
            raise body
        return body

    return pdf_transport, calls


def _extract(path: Path) -> tuple[str, str]:
    assert path.read_bytes().startswith(b"%PDF-")
    return PDF_TEXT, "pdftotext version 24.02.0"


def _paywalled_transport(work: dict | None, root: str | None = '{"version": "0.1"}'):
    base, _ = _transport({"DOI": _record(pmcid=None, open_access="N")})
    return _with_openalex(base, work, root)


def test_openalex_published_pdf_is_stored_with_licence_version_and_api_version(tmp_path: Path) -> None:
    store = _store(tmp_path)
    transport, _ = _paywalled_transport(_work(_location("https://a/x.pdf")))
    pdfs, _ = _pdfs({"https://a/x.pdf": PDF})
    (result,) = lf.fetch_reading_list(["10.1000/x"], store, transport, pdf_transport=pdfs, extract_pdf=_extract)
    assert result.outcome == "fetched" and result.detail == "openalex:https://a/x.pdf"
    rec = store.lookup("doi:10.1000/x")
    assert rec is not None and rec.status == "full_text" and rec.licence == "cc-by"
    assert rec.text_version == "publishedVersion" and rec.extractor == "pdftotext version 24.02.0"
    assert rec.resolver == {
        "service": "openalex",
        "api_version": "0.1",
        "work_id": "https://openalex.org/W1",
        "work_updated_date": "2026-09-01T00:00:00",
        "landing_page_url": "https://pub.example/x",
    }
    assert rec.source_sha256 == hashlib.sha256(PDF).hexdigest()
    assert rec.pdf_path and store.resolve(rec.pdf_path).read_bytes() == PDF and rec.pdf_path.startswith("pdf/")


def test_openalex_missing_licence_is_recorded_as_absent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    transport, _ = _paywalled_transport(_work(_location("https://a/x.pdf", licence=None)))
    pdfs, _ = _pdfs({"https://a/x.pdf": PDF})
    lf.fetch_reading_list(["10.1000/x"], store, transport, pdf_transport=pdfs, extract_pdf=_extract)
    assert store.lookup("doi:10.1000/x").licence is None


def test_openalex_accepted_or_submitted_versions_are_never_downloaded(tmp_path: Path) -> None:
    store = _store(tmp_path)
    transport, _ = _paywalled_transport(
        _work(_location("https://a/acc.pdf", "acceptedVersion"), _location("https://a/sub.pdf", "submittedVersion"))
    )
    pdfs, pdf_calls = _pdfs({})
    (result,) = lf.fetch_reading_list(["10.1000/x"], store, transport, pdf_transport=pdfs, extract_pdf=_extract)
    assert result.outcome == "paywalled" and pdf_calls == []
    assert "no publishedVersion PDF on OpenAlex" in result.detail
    assert store.lookup("doi:10.1000/x").status == "not_available"


def test_openalex_unknown_work_is_paywalled_with_its_reason(tmp_path: Path) -> None:
    store = _store(tmp_path)
    transport, _ = _paywalled_transport(None)
    pdfs, _ = _pdfs({})
    (result,) = lf.fetch_reading_list(["10.1000/x"], store, transport, pdf_transport=pdfs, extract_pdf=_extract)
    assert result.outcome == "paywalled" and "unknown to OpenAlex" in result.detail


def test_openalex_skips_a_non_pdf_body_and_takes_the_next_location(tmp_path: Path) -> None:
    store = _store(tmp_path)
    transport, _ = _paywalled_transport(_work(_location("https://a/landing"), _location("https://b/x.pdf")))
    pdfs, pdf_calls = _pdfs({"https://a/landing": b"<html>login</html>", "https://b/x.pdf": PDF})
    (result,) = lf.fetch_reading_list(["10.1000/x"], store, transport, pdf_transport=pdfs, extract_pdf=_extract)
    assert result.outcome == "fetched" and pdf_calls == ["https://a/landing", "https://b/x.pdf"]
    assert store.lookup("doi:10.1000/x").source_url == "https://b/x.pdf"


def test_openalex_all_locations_failing_records_nothing_and_names_each_cause(tmp_path: Path) -> None:
    store = _store(tmp_path)
    transport, _ = _paywalled_transport(_work(_location("https://a/x.pdf"), _location("https://b/x.pdf")))
    pdfs, _ = _pdfs({"https://a/x.pdf": OSError("403 Forbidden"), "https://b/x.pdf": b"<html></html>"})
    (result,) = lf.fetch_reading_list(["10.1000/x"], store, transport, pdf_transport=pdfs, extract_pdf=_extract)
    assert result.outcome == "failed" and store.lookup("doi:10.1000/x") is None
    assert "https://a/x.pdf: 403 Forbidden" in result.detail and "https://b/x.pdf: not a PDF" in result.detail


def test_openalex_extraction_failure_is_a_failure_and_leaves_no_pdf(tmp_path: Path) -> None:
    store = _store(tmp_path)
    transport, _ = _paywalled_transport(_work(_location("https://a/x.pdf")))
    pdfs, _ = _pdfs({"https://a/x.pdf": PDF})

    def broken(path: Path) -> tuple[str, str]:
        raise ExtractionError("pdftotext failed")

    (result,) = lf.fetch_reading_list(["10.1000/x"], store, transport, pdf_transport=pdfs, extract_pdf=broken)
    assert result.outcome == "failed" and store.lookup("doi:10.1000/x") is None
    assert not list((store.root / "pdf").glob("*.pdf"))


def test_openalex_lookup_error_is_a_failure_not_a_paywall(tmp_path: Path) -> None:
    store = _store(tmp_path)
    base, _ = _transport({"DOI": _record(pmcid=None, open_access="N")})

    def transport(url: str) -> str:
        if url.startswith(lf.OPENALEX):
            raise OSError("timed out")
        return base(url)

    (result,) = lf.fetch_reading_list(["10.1000/x"], store, transport, pdf_transport=_pdfs({})[0])
    assert result.outcome == "failed" and store.lookup("doi:10.1000/x") is None


def test_openalex_unreadable_api_version_is_recorded_as_none(tmp_path: Path) -> None:
    store = _store(tmp_path)
    transport, _ = _paywalled_transport(_work(_location("https://a/x.pdf")), root=None)
    pdfs, _ = _pdfs({"https://a/x.pdf": PDF})
    (result,) = lf.fetch_reading_list(["10.1000/x"], store, transport, pdf_transport=pdfs, extract_pdf=_extract)
    assert result.outcome == "fetched" and store.lookup("doi:10.1000/x").resolver["api_version"] is None


def test_openalex_downloads_consume_the_cap_and_the_api_version_is_read_once(tmp_path: Path) -> None:
    store = _store(tmp_path)
    records = {
        f"10.1000/p{i}": _record(pmcid=None, open_access="N", doi=f"10.1000/p{i}", pmid=str(10000 + i))
        for i in range(3)
    }
    base, _ = _transport(records)
    transport, oa_calls = _with_openalex(base, _work(_location("https://a/x.pdf")))
    pdfs, _ = _pdfs({"https://a/x.pdf": PDF})
    entries = ["10.1000/p0", "10.1000/p1", "10.1000/p2"]
    results = lf.fetch_reading_list(entries, store, transport, max_new=2, pdf_transport=pdfs, extract_pdf=_extract)
    assert [r.outcome for r in results] == ["fetched", "fetched", "skipped_over_cap"]
    assert store.lookup("doi:10.1000/p2") is None
    assert sum(1 for u in oa_calls if "/works/" not in u) == 1


def test_openalex_is_asked_by_pmid_when_europe_pmc_gives_no_doi(tmp_path: Path) -> None:
    store = _store(tmp_path)
    base, _ = _transport({"PMCID": _record(pmcid="PMC7000", open_access="N", doi="")})
    transport, oa_calls = _with_openalex(base, _work(_location("https://a/x.pdf")))
    pdfs, _ = _pdfs({"https://a/x.pdf": PDF})
    (result,) = lf.fetch_reading_list(["PMC7000"], store, transport, pdf_transport=pdfs, extract_pdf=_extract)
    assert result.outcome == "fetched" and any("/works/pmid:11100" in u for u in oa_calls)


def test_without_pdf_transport_openalex_is_never_asked(tmp_path: Path) -> None:
    store = _store(tmp_path)
    transport, oa_calls = _paywalled_transport(_work(_location("https://a/x.pdf")))
    (result,) = lf.fetch_reading_list(["10.1000/x"], store, transport)
    assert result.outcome == "paywalled" and oa_calls == []
    assert result.detail == "no open-access full text on Europe PMC"


def test_store_reads_records_written_before_the_new_fields(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.root.mkdir(parents=True)
    old = {
        "canonical_id": "doi:10.1000/old",
        "aliases": ["doi:10.1000/old"],
        "alias": None,
        "title": "Old",
        "year": None,
        "source_url": None,
        "licence": None,
        "access_date": "2026-01-01T00:00:00Z",
        "status": "not_available",
        "text_path": None,
        "pdf_path": None,
        "sha256": None,
        "extractor": None,
        "run_id": None,
    }
    store.index_path.write_text(json.dumps(old) + "\n")
    rec = store.lookup("doi:10.1000/old")
    assert rec is not None and rec.text_version is None and rec.resolver is None


# --- a refused request is retried, and no transport error escapes  --------------------


def _http_error(code: int, url: str = "https://x", retry_after: str | None = None) -> urllib.error.HTTPError:
    headers = email.message.Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError(url, code, "refused", hdrs=headers, fp=None)  # type: ignore[arg-type]


def _answers(sequence: list):
    """A transport that returns or raises the next item of `sequence`, recording every call."""
    calls: list[str] = []
    remaining = list(sequence)

    def transport(url: str):
        calls.append(url)
        item = remaining.pop(0) if remaining else sequence[-1]
        if isinstance(item, Exception):
            raise item
        return item

    return transport, calls


def _waits():
    taken: list[float] = []
    return taken, taken.append


def test_a_refused_request_is_retried_and_a_404_is_not() -> None:
    """429 and 503 are a "come back later"; 404 is an answer and 403 is a refusal to respect (LICENSES.md)."""
    for code, expected_calls in ((429, lf.MAX_ATTEMPTS), (503, lf.MAX_ATTEMPTS), (404, 1), (403, 1)):
        inner, calls = _answers([_http_error(code)])
        taken, sleep = _waits()
        with pytest.raises(urllib.error.HTTPError):
            lf.Retrying(inner, sleep=sleep, rng=random.Random(1))("https://x")
        assert len(calls) == expected_calls, code
        assert len(taken) == expected_calls - 1, code


def test_a_refusal_that_clears_is_not_an_error_and_the_retries_are_counted() -> None:
    inner, calls = _answers([_http_error(429), "ok"])
    taken, sleep = _waits()
    retrying = lf.Retrying(inner, sleep=sleep, rng=random.Random(1))
    assert retrying("https://x") == "ok"
    assert len(calls) == 2 and retrying.retries == 1 and len(taken) == 1


def test_retry_after_is_honoured_in_both_forms_and_refused_when_it_is_too_long() -> None:
    """A server may answer with seconds or an HTTP-date (RFC 9110); both are read. A wait longer than the cap
    is honoured by giving up: a paid agent session is waiting on the call."""
    now = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
    inner, _ = _answers([_http_error(429, retry_after="2"), "ok"])
    taken, sleep = _waits()
    lf.Retrying(inner, sleep=sleep, now=lambda: now)("https://x")
    assert taken == [2.0]

    http_date = email.utils.format_datetime(now + timedelta(seconds=5))
    inner, _ = _answers([_http_error(429, retry_after=http_date), "ok"])
    taken, sleep = _waits()
    lf.Retrying(inner, sleep=sleep, now=lambda: now)("https://x")
    assert taken == [5.0]

    inner, calls = _answers([_http_error(429, retry_after="600")])
    taken, sleep = _waits()
    with pytest.raises(lf.FetchError, match="exceeds"):
        lf.Retrying(inner, sleep=sleep, now=lambda: now)("https://x")
    assert len(calls) == 1 and taken == [], "a wait beyond the cap is not slept through"


def test_an_unreadable_retry_after_falls_back_to_the_jitter_rather_than_inventing_a_wait() -> None:
    now = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
    assert lf.retry_after_seconds(_http_error(429, retry_after="soon"), lambda: now) is None
    assert lf.retry_after_seconds(_http_error(429), lambda: now) is None
    # A date already past asks for no wait at all, not a negative one.
    past = email.utils.format_datetime(now - timedelta(seconds=30))
    assert lf.retry_after_seconds(_http_error(429, retry_after=past), lambda: now) == 0.0


def test_the_jitter_is_bounded_and_grows() -> None:
    """Full jitter, because the agents of a phase are concurrent: a deterministic schedule would have them all
    come back in the same instant."""
    inner, _ = _answers([_http_error(503), _http_error(503), "ok"])
    taken, sleep = _waits()
    lf.Retrying(inner, attempts=3, sleep=sleep, rng=random.Random(7))("https://x")
    assert len(taken) == 2
    assert all(0.0 <= w <= lf.BACKOFF_CAP_S for w in taken)


def test_a_rate_limited_search_is_reported_as_failed_and_never_raised(tmp_path: Path) -> None:
    """The defect this change exists for. `resolve` called its transport outside every `try`, and the only
    handler around it caught `FetchError` alone, so an `HTTPError` escaped `fetch_reading_list`,
    `provenance.fetch_missing`, the orchestrator's handler and the CLI's: a 429 on the search leg killed the
    run mid-phase, with the paid agent session for that phase already spent."""
    inner, calls = _answers([_http_error(429, url=f"{lf.EUROPE_PMC}/search")])
    results = lf.fetch_reading_list(["10.1000/x"], _store(tmp_path), inner, run_id="r")
    (row,) = results
    assert row.outcome == "failed" and "429" in row.detail
    assert row.retries == lf.MAX_ATTEMPTS - 1
    assert len(calls) == lf.MAX_ATTEMPTS


@pytest.mark.parametrize("error", [OSError("timed out"), urllib.error.URLError("name not resolved")])
def test_no_transport_error_on_the_search_leg_escapes_the_reading_list(tmp_path: Path, error) -> None:
    """Not only a 429: a timeout or a DNS failure took the same path. "One `EntryResult` per entry" is the
    function's own promise and it now holds for every leg."""
    inner, _ = _answers([error])
    (row,) = lf.fetch_reading_list(["10.1000/x"], _store(tmp_path), inner, run_id="r")
    assert row.outcome == "failed" and row.retries == 0


def test_a_clean_fetch_reports_no_retries(tmp_path: Path) -> None:
    """Zero is the normal case: `retries` needs no interpretation to be read."""
    transport, _ = _transport({"10.1000/x": _record()}, {"PMC1001": JATS})
    (row,) = lf.fetch_reading_list(["10.1000/x"], _store(tmp_path), transport, run_id="r")
    assert row.outcome == "fetched" and row.retries == 0


def test_a_rate_limited_pdf_location_is_retried_before_the_next_one_is_tried(tmp_path: Path) -> None:
    """Leg 3b: the per-location loop swallowed a 429 and moved on, so one refusal could burn every location
    of a work in a tight loop."""
    work = _work(_location("https://a.example/one.pdf"), _location("https://b.example/two.pdf"))
    base, _ = _paywalled_transport(work)
    pdf, pdf_calls = _pdfs(
        {
            "https://a.example/one.pdf": _http_error(429, url="https://a.example/one.pdf"),
            "https://b.example/two.pdf": b"%PDF-1.7 body",
        }
    )
    (row,) = lf.fetch_reading_list(
        ["10.1000/x"], _store(tmp_path), base, run_id="r", pdf_transport=pdf, extract_pdf=_extract
    )
    assert row.outcome == "fetched" and row.retries == lf.MAX_ATTEMPTS - 1
    assert pdf_calls.count("https://a.example/one.pdf") == lf.MAX_ATTEMPTS


def test_the_real_urllib_path_reads_retry_after_from_a_real_server(tmp_path: Path) -> None:
    """`http_transport` is the module's deliberately untested seam (mock the
    network), and the retry decision was factored out of it for that reason. But one thing cannot be checked
    with a fake: that `urllib` exposes the `Retry-After` header where `retry_after_seconds` looks for it. A
    header name or a parse is the likely bug here, so this drives the real client against a local server.
    No network: it listens on 127.0.0.1 and the test owns it.
    """
    import http.server
    import threading

    refusals = {"left": 2}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - the base class's name
            if refusals["left"] > 0:
                refusals["left"] -= 1
                self.send_response(429)
                self.send_header("Retry-After", "0")
                self.end_headers()
                return
            body = b"served"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args) -> None:  # keep the test output clean
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        taken, sleep = _waits()
        retrying = lf.Retrying(lf.http_transport, sleep=sleep)
        assert retrying(f"http://127.0.0.1:{server.server_port}/x") == "served"
        # Two refusals, two waits, and each is the zero the server asked for — not the jitter, which proves
        # the header was read rather than ignored.
        assert retrying.retries == 2 and taken == [0.0, 0.0]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
