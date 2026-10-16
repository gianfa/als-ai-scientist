"""Fetch open-access full texts from Europe PMC into the literature store (step b of the reading pool).

`fetch_reading_list` takes the planner's reading list and, for every entry, decides one of:

- `cached`: the paper is already in the store (text present and sha256 intact); no network, no cap use.
- `known_unavailable`: an earlier run recorded that the text is not available; not retried by default.
- `unresolvable`: the identifier is not recognisable or Europe PMC does not know it. Nothing is invented
  or substituted.
- `paywalled`: Europe PMC knows the paper but has no open-access full text, and the OpenAlex fallback (when
  enabled) found no usable PDF either; recorded as `not_available`.
- `skipped_over_cap`: open-access text exists but `max_new` new papers were already fetched in this run;
  nothing is recorded, so a later run (or a higher cap) picks the paper up.
- `failed`: the text download or its parsing failed; nothing is recorded. A request refused with 429 or 503
  is retried first (`Retrying`: at most `MAX_ATTEMPTS` requests, `Retry-After` honoured up to
  `RETRY_AFTER_CAP_S` and otherwise a jittered backoff), so `failed` means the refusal survived the retries.
  **No transport error escapes this function**: "one `EntryResult` per entry" is a promise, and until
  then the Europe PMC search leg broke it — an `HTTPError` or a timeout there propagated out of the
  orchestrator and killed the run mid-phase. Each result carries `retries`, the extra requests that entry cost.
- `fetched`: the text was downloaded, converted and added to the store. Only these consume the cap.

OpenAlex fallback, used only when a `pdf_transport` is given: for a paper Europe PMC knows but cannot serve, the
OpenAlex work is looked up by DOI (else PMID) and its `publishedVersion` PDF locations are tried in order. The
first body that is a PDF and yields text is stored with `pdftotext`, its licence as OpenAlex reports it,
`text_version="publishedVersion"` and a `resolver` record naming OpenAlex, its API version (`GET /`), the work
id and the work's `updated_date`. Accepted and submitted manuscripts are never used: they are not the text the
citation refers to. Calls are keyless (OpenAlex's smaller anonymous daily budget, enough for a reading list): an
API key would travel in the URL, and URLs end up in results and run logs.

Network access goes through injected transports (`url -> body`), so tests run offline.

Limits, stated rather than hidden:
- Europe PMC open-access JATS XML is the primary source. The main text keeps titles, abstract and body paragraphs
  (with "Fig. 2b-d" style cross-references); bibliography markers are dropped. Tables, figure captions, formulas and
  supplements are kept apart as `extras`, the reference list as `refs`, so a quote found only there can be told from one
  that is not in the paper.
- Licence is whatever Europe PMC or OpenAlex reports; it is recorded, not verified, and OpenAlex may report none.
- A text from a PDF loses tables, captions and layout; `tools/literature/mcp_server.py` treats every non-JATS
  text as partial, so a quote missing from it is reported as possibly incomplete, not as invented.
- OpenAlex is not asked about papers Europe PMC does not know (`unresolvable` stays as it is).
- Existence is confirmed, relevance is not: a fetched paper may be useless for the question.
"""

from __future__ import annotations

import email.utils
import hashlib
import json
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Final, Iterable, Mapping, TypeVar

from tools.literature.store import ExtractionError, LiteratureStore, canonical_id, extract_pdf_text

EUROPE_PMC = "https://www.ebi.ac.uk/europepmc/webservices/rest"
OPENALEX = "https://api.openalex.org"
OPENALEX_TEXT_VERSION = "publishedVersion"  # the only OpenAlex location version the fallback accepts
DEFAULT_MAX_NEW = 5
OUTCOMES = ("fetched", "cached", "known_unavailable", "unresolvable", "paywalled", "skipped_over_cap", "failed")
EXTRACTOR = "europepmc-jats-xml v2"

# Retrying a refused request. Europe PMC applies "10 requests per second and 500 requests per minute", "per IP
# address" (its staff on the developer forum; no published page states it — see LICENSES.md), and OpenAlex
# answers 429 above its daily budget or 100 requests per second. The host and every agent of a phase share one
# IP, so since `fetch_paper` up to a dozen agent-side fetches per run draw on the same bucket.
#
# Only these two statuses. **403 is never retried**: a host refusing a robot is a refusal to respect, not an
# obstacle to route around, and three were observed in the 2026-10-10 trial (Lancet, JAMA). Nor is 404, which
# is an answer.
RETRY_STATUSES: Final = (429, 503)
MAX_ATTEMPTS: Final = 3  # the first try plus two retries
BACKOFF_BASE_S: Final = 1.0
BACKOFF_CAP_S: Final = 8.0
# A `Retry-After` longer than this is honoured by giving up rather than by sleeping: a paid agent session is
# waiting on the call, and spending ten minutes of it asleep is worse than reporting the failure.
RETRY_AFTER_CAP_S: Final = 30.0

# Full jitter, not plain exponential: the agents of a phase are concurrent by construction, so a deterministic
# schedule would have them all retry in the same instant. Its own generator, as every other `random` use in
# this repository has (`analysis/a1_immunodominance.py`), and unseeded on purpose — the wait enters no result,
# no record and no hash, so nothing it touches has to be reproducible.
_JITTER = random.Random()

Transport = Callable[[str], str]
BytesTransport = Callable[[str], bytes]
PdfExtractor = Callable[[Path], tuple[str, str]]

# Kept out of the main text. Tables, figures, formulas and supplements go to `extras`; bibliography markers
# (`xref ref-type="bibr"`) are dropped; other cross-references ("Fig. 2b-d") are prose and stay.
_EXTRAS_TAGS = {"table-wrap", "fig", "disp-formula", "inline-formula", "supplementary-material"}
_SKIP_TAGS = _EXTRAS_TAGS | {"label"}
_BLOCK_TAGS = {"article-title", "title", "p"}
_STOPWORDS = {"a", "an", "the", "of", "on", "in", "and", "for", "to", "with"}


class FetchError(RuntimeError):
    """The service answered, but not with something usable."""


@dataclass(frozen=True)
class EntryResult:
    identifier: str
    canonical_id: str | None
    outcome: str
    detail: str = ""
    retries: int = 0  # extra requests this entry cost because a 429 or 503 was retried


T = TypeVar("T")


def retry_after_seconds(error: urllib.error.HTTPError, now: Callable[[], datetime]) -> float | None:
    """The wait the server asked for, in seconds, or None when it asked for none or asked unreadably.

    `Retry-After` takes either a number of seconds or an HTTP-date (RFC 9110), and servers send both, so both
    are read. A past date, a negative number or anything unparseable is None: a header we cannot trust is not
    a reason to invent a wait.
    """
    raw = (error.headers or {}).get("Retry-After") if hasattr(error, "headers") else None
    if raw is None:
        return None
    text = str(raw).strip()
    try:
        return max(float(text), 0.0)
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max((when - now()).total_seconds(), 0.0)


class Retrying:
    """A transport that retries a **refused** request, and counts what the refusals cost.

    Wrapped around the injected transport rather than built into `http_transport`, for three reasons: the
    fake transports of the tests exercise this logic with no network at all; `http_transport` stays the plain
    one-shot GET its docstring promises, so the module's deliberately untested seam gains nothing; and any
    caller injecting its own transport is covered without asking for it.

    `retries` counts the **extra** requests refusals caused, which is what
    the provenance rules mean by recording "errors/retries": zero is the normal case
    and needs no interpretation. `fetch_reading_list` takes the difference per entry.
    """

    def __init__(
        self,
        inner: Callable[[str], T],
        *,
        attempts: int = MAX_ATTEMPTS,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._inner, self._attempts, self._sleep = inner, max(attempts, 1), sleep
        self._rng, self._now = rng or _JITTER, now
        self.retries = 0
        self.waited: list[float] = []

    def __call__(self, url: str) -> Any:
        for attempt in range(self._attempts):
            try:
                return self._inner(url)
            except urllib.error.HTTPError as err:
                last = attempt == self._attempts - 1
                if err.code not in RETRY_STATUSES or last:
                    raise
                asked = retry_after_seconds(err, self._now)
                if asked is not None and asked > RETRY_AFTER_CAP_S:
                    # Honoured by giving up: the server named a wait longer than this call may spend asleep.
                    raise FetchError(
                        f"{url}: HTTP {err.code}, Retry-After {asked:.0f}s exceeds the "
                        f"{RETRY_AFTER_CAP_S:.0f}s this call may wait"
                    ) from err
                wait = (
                    asked
                    if asked is not None
                    else self._rng.uniform(0.0, min(BACKOFF_CAP_S, BACKOFF_BASE_S * 2**attempt))
                )
                self.retries += 1
                self.waited.append(wait)
                self._sleep(wait)
        raise AssertionError("unreachable: the last attempt either returns or raises")


def http_transport(url: str, timeout: float = 60.0) -> str:
    """GET `url` and return the body (raises on HTTP errors; `Retrying` decides whether to try again)."""
    request = urllib.request.Request(url, headers={"User-Agent": "als-ai-scientist-literature-fetch/1"})
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


def http_bytes_transport(url: str, timeout: float = 60.0) -> bytes:
    """GET `url` and return the raw body (raises on HTTP errors); for PDFs. See `Retrying`."""
    request = urllib.request.Request(url, headers={"User-Agent": "als-ai-scientist-literature-fetch/1"})
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        return resp.read()


def _openalex_url(path: str) -> str:
    return f"{OPENALEX}/{path}"


def openalex_api_version(transport: Transport) -> str | None:
    """The `version` OpenAlex declares at its root; None when it cannot be read (recorded as such, not guessed)."""
    try:
        value = json.loads(transport(_openalex_url("")))["version"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        return None
    return str(value) if value else None


def openalex_work(identifiers: Iterable[str], transport: Transport) -> tuple[dict[str, Any] | None, str]:
    """The OpenAlex work for the first DOI (else PMID) among `identifiers`, or (None, why it is absent).

    Raises `FetchError` when OpenAlex could not be asked or answered with something unusable: that is a failure
    to retry, not evidence that the paper has no open copy.
    """
    ids = list(identifiers)
    key = next((f"https://doi.org/{i[4:]}" for i in ids if i.startswith("doi:")), None) or next(
        (i for i in ids if i.startswith("pmid:")), None
    )
    if key is None:
        return None, "no DOI or PMID to ask OpenAlex with"
    url = _openalex_url("works/" + urllib.parse.quote(key, safe=":/"))
    try:
        body = transport(url)
    except urllib.error.HTTPError as err:
        if err.code == 404:
            return None, "unknown to OpenAlex"
        raise FetchError(f"{url}: {err}") from err
    except OSError as err:
        raise FetchError(f"{url}: {err}") from err
    try:
        work = json.loads(body)
    except json.JSONDecodeError as err:
        raise FetchError(f"unexpected OpenAlex response for {key}: {err}") from err
    if not isinstance(work, dict):
        raise FetchError(f"unexpected OpenAlex response for {key}: not an object")
    return work, ""


def published_pdf_locations(work: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The work's locations with a PDF URL and `version == publishedVersion`, best OA location first, deduplicated."""
    found: dict[str, dict[str, Any]] = {}
    for location in [work.get("best_oa_location"), *(work.get("locations") or [])]:
        if not isinstance(location, dict):
            continue
        url = location.get("pdf_url")
        if url and location.get("version") == OPENALEX_TEXT_VERSION and url not in found:
            found[url] = location
    return list(found.values())


def search_query(canonical: str) -> str:
    """The Europe PMC query for a canonical identifier."""
    kind, _, value = canonical.partition(":")
    if kind == "doi":
        return f'DOI:"{value}"'
    if kind == "pmid":
        return f"EXT_ID:{value} AND SRC:MED"
    if kind == "pmcid":
        return f"PMCID:{value}"
    raise ValueError(f"unsupported identifier: {canonical}")


def resolve(canonical: str, transport: Transport) -> dict[str, Any] | None:
    """The Europe PMC core record of the paper; None when Europe PMC does not know it."""
    params = urllib.parse.urlencode(
        {"query": search_query(canonical), "format": "json", "resultType": "core", "pageSize": 1}
    )
    url = f"{EUROPE_PMC}/search?{params}"
    # Previously this call sat outside every `try`, and the only handler around `resolve` catches
    # `FetchError` alone. An `HTTPError` (an `OSError`) therefore escaped `fetch_reading_list`,
    # `provenance.fetch_missing`, the orchestrator's `except (ValidationError, ValueError)` and the CLI's
    # `except (PhaseFailed, ValueError)`: a 429 or a DNS blip on the search leg killed the run mid-phase, with
    # the paid agent session for that phase already spent. It never fired in 84 archived attempts; `fetch_paper`
    # multiplies the calls per run by about four. `openalex_work` already did this.
    try:
        body = transport(url)
    except FetchError:
        raise
    except OSError as err:
        raise FetchError(f"{url}: {err}") from err
    try:
        results = json.loads(body)["resultList"]["result"]
    except (json.JSONDecodeError, KeyError, TypeError) as err:
        raise FetchError(f"unexpected Europe PMC search response for {canonical}: {err}") from err
    return results[0] if results else None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _skipped(element: ET.Element) -> bool:
    tag = _local(element.tag)
    return tag in _SKIP_TAGS or (tag == "xref" and element.get("ref-type") == "bibr")


def _text(element: ET.Element) -> str:
    parts: list[str] = []
    if not _skipped(element):
        parts.append(element.text or "")
        for child in element:
            parts.append(_text(child))
            parts.append(child.tail or "")
    return "".join(parts)


@dataclass(frozen=True)
class JatsParts:
    """The texts of a JATS article. `main` is what the quote check reads first; `extras` (tables, figure captions, formulas,
    supplements) and `refs` (reference list) are consulted only to tell a quote that is missing from the stored text from one
    that is not in the paper."""

    main: str
    extras: str
    refs: str


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def jats_parts(xml_body: str) -> JatsParts:
    """Split a JATS article into main text (title, abstract, body paragraphs), extras and references."""
    try:
        root = ET.fromstring(xml_body)
    except ET.ParseError as err:
        raise FetchError(f"full text is not valid XML: {err}") from err
    blocks: list[str] = []
    extras: list[str] = []
    refs: list[str] = []

    def walk(element: ET.Element) -> None:
        tag = _local(element.tag)
        if _skipped(element):
            return
        if tag in _BLOCK_TAGS:
            block = _squash(_text(element))
            if block:
                blocks.append(block)
            return
        for child in element:
            walk(child)

    def collect(element: ET.Element) -> None:
        tag = _local(element.tag)
        target = refs if tag == "ref-list" else extras if tag in _EXTRAS_TAGS else None
        if target is not None:
            text = _squash(" ".join(element.itertext()))
            if text:
                target.append(text)
            return
        for child in element:
            collect(child)

    walk(root)
    collect(root)

    def join(parts: list[str]) -> str:
        return "\n\n".join(parts) + "\n" if parts else ""

    return JatsParts(join(blocks), join(extras), join(refs))


def jats_to_text(xml_body: str) -> str:
    """The main text of a JATS article (see `jats_parts`), one block per blank line."""
    return jats_parts(xml_body).main


def make_alias(record: Mapping[str, Any]) -> str | None:
    """`<first-author>-<year>-<first significant title word>`; None when the metadata does not allow it."""
    authors = str(record.get("authorString") or "").strip()
    year = record.get("pubYear")
    title = str(record.get("title") or "")
    if not authors or not year:
        return None
    surname = re.sub(r"[^a-z0-9]+", "", authors.split(",")[0].split()[0].lower())
    words = [w for w in re.findall(r"[a-z0-9]+", title.lower()) if w not in _STOPWORDS]
    if not surname or not words:
        return None
    return f"{surname}-{year}-{words[0]}"


def _licence(record: Mapping[str, Any]) -> str | None:
    value = record.get("license")
    return str(value) if value else None


def _store_openalex_pdf(
    store: LiteratureStore,
    ids: list[str],
    record: Mapping[str, Any],
    title: str,
    work: Mapping[str, Any],
    locations: list[dict[str, Any]],
    api_version: str | None,
    pdf_transport: BytesTransport,
    extract_pdf: PdfExtractor,
    run_id: str | None,
) -> tuple[bool, str]:
    """Try the locations in order; (True, url) once one is stored, else (False, every attempt and its cause)."""
    attempts: list[str] = []
    for location in locations:
        url = str(location["pdf_url"])
        try:
            body = pdf_transport(url)
        except OSError as err:
            attempts.append(f"{url}: {err}")
            continue
        if not body.startswith(b"%PDF-"):
            attempts.append(f"{url}: not a PDF")
            continue
        path = store.save_pdf(ids[0], body)
        try:
            text, extractor = extract_pdf(path)
        except ExtractionError as err:
            path.unlink(missing_ok=True)
            attempts.append(f"{url}: {err}")
            continue
        year = int(record["pubYear"]) if str(record.get("pubYear") or "").isdigit() else None
        resolver = {
            "service": "openalex",
            "api_version": api_version,
            "work_id": work.get("id"),
            "work_updated_date": work.get("updated_date"),
            "landing_page_url": location.get("landing_page_url"),
        }
        store.add(
            identifiers=ids,
            title=title,
            year=year,
            text=text,
            source_url=url,
            licence=location.get("license"),
            extractor=extractor,
            run_id=run_id,
            alias=make_alias(record),
            pdf_path=str(path),
            source_sha256=hashlib.sha256(body).hexdigest(),
            text_version=OPENALEX_TEXT_VERSION,
            resolver=resolver,
        )
        return True, url
    return False, "; ".join(attempts)


def fetch_reading_list(
    entries: Iterable[Mapping[str, Any] | str],
    store: LiteratureStore,
    transport: Transport,
    *,
    max_new: int = DEFAULT_MAX_NEW,
    run_id: str | None = None,
    retry_unavailable: bool = False,
    pdf_transport: BytesTransport | None = None,
    extract_pdf: PdfExtractor = extract_pdf_text,
) -> list[EntryResult]:
    """Bring the listed papers into the store; one `EntryResult` per entry, in the order given.

    `max_new` caps the papers newly downloaded in this call (priority = list order). Cached, paywalled,
    unresolvable and failed entries do not consume it. `pdf_transport` enables the OpenAlex fallback (module
    docstring); without it no OpenAlex call is made.
    """
    if max_new < 0:
        raise ValueError("max_new must be >= 0")
    # Refusals are retried here, around the injected transports, so a fake transport in a test exercises the
    # same logic and `http_transport` stays a one-shot GET (`Retrying`). The counters give each entry the
    # retries it cost.
    transport = Retrying(transport)
    pdf_transport = Retrying(pdf_transport) if pdf_transport is not None else None
    results: list[EntryResult] = []
    new = 0
    seen: set[str] = set()
    api_version: str | None = None
    api_version_read = False
    spent = 0  # retries already attributed to earlier entries

    def tally(result: EntryResult) -> EntryResult:
        """The entry's result, carrying the retries this entry alone cost (across all of its legs).

        Not named `record`: the loop below binds that name to the Europe PMC core record.
        """
        nonlocal spent
        total = transport.retries + (pdf_transport.retries if pdf_transport is not None else 0)
        mine, spent = total - spent, total
        return EntryResult(result.identifier, result.canonical_id, result.outcome, result.detail, retries=mine)

    for entry in entries:
        raw = entry if isinstance(entry, str) else str(entry.get("identifier") or "")
        title = "" if isinstance(entry, str) else str(entry.get("title") or "")
        canonical = canonical_id(raw)
        if canonical is None:
            results.append(tally(EntryResult(raw, None, "unresolvable", "not a DOI, PMID or PMCID")))
            continue
        if canonical in seen:
            continue  # a duplicate in the list: one result only
        seen.add(canonical)
        existing = store.lookup(canonical)
        if existing is not None and existing.status != "not_available":
            results.append(tally(EntryResult(raw, existing.canonical_id, "cached", existing.status)))
            continue
        if existing is not None and not retry_unavailable:
            results.append(tally(EntryResult(raw, existing.canonical_id, "known_unavailable", existing.note or "")))
            continue
        try:
            record = resolve(canonical, transport)
        except FetchError as err:
            results.append(tally(EntryResult(raw, canonical, "failed", str(err))))
            continue
        if record is None:
            results.append(tally(EntryResult(raw, canonical, "unresolvable", "unknown to Europe PMC")))
            continue
        ids = [
            canonical,
            *(f"{k}:{record[f]}" for f, k in (("pmid", "pmid"), ("pmcid", "pmcid"), ("doi", "doi")) if record.get(f)),
        ]
        record_title = str(record.get("title") or title or canonical)
        pmcid = record.get("pmcid")
        if record.get("isOpenAccess") != "Y" or not pmcid:
            reason = "no open-access full text on Europe PMC"
            work: dict[str, Any] | None = None
            locations: list[dict[str, Any]] = []
            if pdf_transport is not None:
                try:
                    work, absent = openalex_work(ids, transport)
                except (FetchError, OSError) as err:
                    results.append(tally(EntryResult(raw, canonical, "failed", f"OpenAlex: {err}")))
                    continue
                locations = published_pdf_locations(work) if work is not None else []
                if not locations:
                    reason += f"; {absent or f'no {OPENALEX_TEXT_VERSION} PDF on OpenAlex'}"
            if work is None or not locations:
                store.add_unavailable(
                    identifiers=ids, title=record_title, run_id=run_id, licence=_licence(record), reason=reason
                )
                results.append(tally(EntryResult(raw, canonical, "paywalled", reason)))
                continue
            if new >= max_new:
                results.append(tally(EntryResult(raw, canonical, "skipped_over_cap", f"max_new={max_new} reached")))
                continue
            if not api_version_read:
                api_version, api_version_read = openalex_api_version(transport), True
            stored, detail = _store_openalex_pdf(
                store, ids, record, record_title, work, locations, api_version, pdf_transport, extract_pdf, run_id
            )
            if not stored:
                results.append(tally(EntryResult(raw, canonical, "failed", f"OpenAlex PDFs: {detail}")))
                continue
            new += 1
            results.append(tally(EntryResult(raw, canonical, "fetched", f"openalex:{detail}")))
            continue
        if new >= max_new:
            results.append(tally(EntryResult(raw, canonical, "skipped_over_cap", f"max_new={max_new} reached")))
            continue
        url = f"{EUROPE_PMC}/{pmcid}/fullTextXML"
        try:
            parts = jats_parts(transport(url))
        except (FetchError, OSError) as err:
            results.append(tally(EntryResult(raw, canonical, "failed", f"{url}: {err}")))
            continue
        year = int(record["pubYear"]) if str(record.get("pubYear") or "").isdigit() else None
        store.add(
            identifiers=ids,
            title=record_title,
            year=year,
            text=parts.main,
            source_url=url,
            licence=_licence(record),
            extractor=EXTRACTOR,
            run_id=run_id,
            alias=make_alias(record),
            extras=parts.extras or None,
            refs=parts.refs or None,
        )
        new += 1
        results.append(tally(EntryResult(raw, canonical, "fetched", str(pmcid))))
    return results


def report(results: Iterable[EntryResult]) -> dict[str, Any]:
    """Counts per outcome plus the entries, for `fetch-report.json`."""
    rows = [asdict(r) for r in results]
    counts = {name: sum(1 for r in rows if r["outcome"] == name) for name in OUTCOMES}
    return {"counts": counts, "entries": rows}


def load_reading_list(path: Path) -> list[Mapping[str, Any] | str]:
    """A JSON list of identifiers/objects, or an object with a `reading_list` list."""
    data = json.loads(path.read_text(encoding="utf-8"))
    entries = data.get("reading_list") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        raise ValueError(f"{path}: expected a list or an object with a 'reading_list' list")
    return entries
