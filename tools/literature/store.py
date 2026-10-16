"""Deterministic store and index for the full text of papers (a knowledge cache).

The store answers two questions without any LLM: "do we already have this paper?" and "what exactly is
its text?". It keeps, under one root directory (default `<repo>/literature/store/`, git-ignored, never
committed):

- `index.jsonl`: an append-only manifest, one record per (re)addition of a paper. Nothing is rewritten;
  for a paper the newest record is used and older ones stay for audit.
- `text/<name>.txt`: the extracted plain text of the paper, checked against the recorded sha256 every time
  it is looked up (a changed file is treated as absent, see `LiteratureStore.lookup`).
- `pdf/<name>.pdf`: the PDFs downloaded by the fetch fallback (`LiteratureStore.save_pdf`); local copies,
  never redistributed.
- `INDEX.md`: a human/agent-readable view *generated* from the index; it is never edited by hand.

Identifiers are canonicalised (`doi:...`, `pmid:...`, `pmcid:PMC...`) so the same paper is found whichever
spelling was used. Network access is not in this module (see `scripts/data/literature_fetch.py`).

Limits, stated rather than hidden:
- The text is what the extractor produced; two-column layouts can interleave lines, tables and figures are
  lost and a scanned PDF yields no text. A store record says "this is the extracted text", not "this is
  what the paper says".
- Licence and access date are recorded as given by the caller; they are not verified here.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Final, Iterable, Iterator

# Resolved from this file, not from the current directory: the same store whatever the caller runs from.
# `literature/` is tracked; only `literature/store/` (and the
# user's `literature/inbox/`) is git-ignored, see literature/README.md.
REPO_ROOT = Path(__file__).resolve().parents[2]  # tools/literature/store.py -> repository root
DEFAULT_ROOT = REPO_ROOT / "literature" / "store"
MIN_FULL_TEXT_CHARS = 2000  # below this the text is "thin": abstract-only, never full text
STATUSES = ("full_text", "abstract_only", "not_available")
AVAILABILITY = ("github", "on-demand", "registration", "no", "unknown")

_DOI = re.compile(r"(10\.\d{4,9}/[^\s\"<>]+)", re.IGNORECASE)
_PMID = re.compile(r"^(?:pmid)?[\s:]*(\d{5,9})$", re.IGNORECASE)
_PMID_URL = re.compile(r"pubmed\.ncbi\.nlm\.nih\.gov/(\d{5,9})", re.IGNORECASE)
_PMCID = re.compile(r"(PMC\d{4,9})", re.IGNORECASE)
# Every article of a Springer Nature journal has the DOI 10.1038/<id> where <id> is the last part of its nature.com URL.
_NATURE_URL = re.compile(r"^https?://(?:www\.)?nature\.com/articles/([A-Za-z0-9._-]+?)/?(?:[?#].*)?$", re.IGNORECASE)


class ExtractionError(RuntimeError):
    """A PDF could not be turned into text."""


def canonical_id(raw: str | None) -> str | None:
    """`doi:<lower>`, `pmid:<digits>` or `pmcid:PMC<digits>`; None when nothing is recognisable."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if m := _NATURE_URL.match(text):
        return "doi:10.1038/" + m.group(1).lower()
    if m := _PMID_URL.search(text):
        return f"pmid:{m.group(1)}"
    if m := _PMCID.search(text):
        return f"pmcid:{m.group(1).upper()}"
    if m := _DOI.search(text):
        return "doi:" + m.group(1).rstrip("/.,;)").lower()
    if m := _PMID.match(text):
        return f"pmid:{m.group(1)}"
    return None


# References that name an artifact of this system rather than a published source. They are provenance of a
# different kind — a receipt, a declared dataset version, an experiment, a computed result — and asking
# whether they are a DOI is a category error.
INTERNAL_REF_PREFIXES = ("receipt:", "dataset:", "experiment:", "result:")


# The declared forms a source reference may take. The point is not purity but **diagnosability**: a typed
# reference says what kind of thing to look for, so a reference this system cannot resolve can still be
# chased later — by hand or by a search — while a bare `41034581` or the sentence `Web search results`
# cannot. Declaring the type also retires the one guess the canonicaliser made: an untyped number is now
# refused rather than read as a PMID.
SOURCE_SCHEMES = ("doi", "pmid", "pmcid", "arxiv", "isbn", "url", "handle", "accession")
_SCHEME_REF = re.compile(r"^(" + "|".join(SOURCE_SCHEMES) + r")\s*:\s*\S.*$", re.IGNORECASE)
_URL_REF = re.compile(r"^https?://\S+$", re.IGNORECASE)

# Those of `SOURCE_SCHEMES` that name a **persistent identifier in a registry**: a string anyone can resolve to
# one document, now or in ten years, and which no news page can be. `url` is deliberately absent — a URL is a
# *location*, not an identifier, and it is exactly the shape the `C-87` pathology takes (a university press
# page standing in for the paper it reports; bug observatory entry 11).
#
# This list exists because `canonical_id` and `is_declared_source` answer different questions and were being
# read as one. `canonical_id` maps a reference into the three identifier spaces the **store** indexes
# (`lookup`, `fetch_reading_list`, `fetch_paper`), so it must stay narrow: widening it would have the fetcher
# ask Europe PMC for an ISBN. But `unresolved_refs` means "names neither a published source nor an artifact of
# this system", and an `arxiv:`/`isbn:`/`handle:`/`accession:` reference plainly does name one. Until
# Earlier it called them unresolved, so the proposal gate would have flagged a correct arXiv citation as
# `cites_an_unresolvable_source` while `SOURCE_FORM_HELP` told the agents to write exactly that. Measured over
# the 1371 references on record when this was fixed: 0 occurrences, so no archive is affected — a trap, not a
# wound. All 16 declared-but-unresolvable references in those archives are bare URLs, which stay flagged.
IDENTIFIER_SCHEMES = ("doi", "pmid", "pmcid", "arxiv", "isbn", "handle", "accession")
_IDENTIFIER_REF = re.compile(r"^(" + "|".join(IDENTIFIER_SCHEMES) + r")\s*:\s*\S.*$", re.IGNORECASE)


def names_an_identifier(ref: str | None) -> bool:
    """Whether `ref` declares a persistent identifier, as opposed to a location or free text.

    A *shape* test, like `is_declared_source`, and narrower than it by exactly one scheme: `url:`.
    """
    return bool(_IDENTIFIER_REF.match((ref or "").strip()))


SOURCE_FORM_HELP = (
    "Declare the kind: `doi:10.xxxx/...`, `PMID:12345678`, `PMCID:PMC1234567`, `arxiv:2401.01234`, or a "
    "full `https://...` URL. A bare number or a sentence is refused, because nobody can look it up later. "
    "If you have no source, leave it empty: the claim is then recorded as the system's own reasoning "
    "rather than as literature."
)


def is_declared_source(ref: str | None) -> bool:
    """Whether a reference declares what kind of identifier it is, or is a full URL.

    This is a *shape* test, not a lookup: it says the reference can be chased, not that it leads anywhere.
    An internal reference (a receipt, a dataset version) is already typed by its own prefix.
    """
    text = (ref or "").strip()
    if not text:
        return False
    return bool(text.startswith(INTERNAL_REF_PREFIXES) or _SCHEME_REF.match(text) or _URL_REF.match(text))


# Shapes that already say what kind of identifier they are. `10.1038/s41586-025-09588-6` can be nothing but
# a DOI and `PMC12657233` nothing but a PMCID, so demanding a `doi:` prefix in front of them buys no
# diagnosability — the stated purpose of the declaration rule — and costs the attribution. A bare
# five-to-nine-digit number is deliberately **not** here: that is the one guess the rule retired, and it
# stays retired. The four shapes below account for most undeclared references in practice; the rest are
# free text (`Web search results`) or a record id (`C-108`), which no pattern can rescue.
_SELF_IDENTIFYING = (_DOI, _PMCID, _PMID_URL, _NATURE_URL)


def recovered_source(ref: str | None) -> str | None:
    """The canonical id of an **undeclared** reference that identifies itself by shape, else None.

    This is the host *recognising* what the agent wrote, not the agent *declaring* it. The two must stay
    distinguishable in the record — the agent's spelling is kept in `source_refs_raw` — because only one of
    them is a statement by the author, and a later audit of whether the agents learned the rule depends on
    being able to tell them apart.
    """
    text = (ref or "").strip()
    if not text or is_declared_source(text):
        return None
    if not any(pattern.search(text) for pattern in _SELF_IDENTIFYING):
        return None
    return canonical_id(text)


def canonicalise_refs(refs: Iterable[str]) -> tuple[list[str], list[str]]:
    """`(canonical, unresolved)` for a list of source references, order and duplicates preserved.

    Canonical is what goes into the record: a recognised identifier in one form, an internal reference
    untouched, and anything else **exactly as it was written**. Nothing is dropped and nothing is
    invented — a reference naming no identifier (`Web search results`, a claim id, a news domain) stays
    in the record and is named in `unresolved`, because deleting it would hide that the claim rests on
    nothing checkable.

    `unresolved` is **not** "what the store cannot index". A reference declaring a persistent identifier the
    store does not index — `arxiv:`, `isbn:`, `handle:`, `accession:` — is resolvable by a reader even though
    `canonical_id` returns None for it, so it is kept as written and **not** named here
    (`IDENTIFIER_SCHEMES`). A bare URL, or one wrapped as `url:`, is a location and stays named.
    """
    canonical: list[str] = []
    unresolved: list[str] = []
    for ref in refs:
        text = (ref or "").strip()
        if not text:
            continue
        if text.startswith(INTERNAL_REF_PREFIXES):
            canonical.append(text)
            continue
        resolved = canonical_id(text)
        canonical.append(resolved or text)
        if resolved is None and not names_an_identifier(text):
            unresolved.append(text)
    return canonical, unresolved


def unresolved_refs(refs: Iterable[str]) -> list[str]:
    """Those of `refs` that name neither a published source nor an artifact of this system.

    "Names" is a shape test over the string: no network and no lookup. It says there is nothing to look up,
    **not** that someone looked and found nothing — a well-formed DOI that leads nowhere passes here.
    """
    return canonicalise_refs(refs)[1]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_name(canonical: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", canonical)


@dataclass(frozen=True)
class SourceRecord:
    canonical_id: str
    aliases: tuple[str, ...]  # every canonical id and the human alias known for this paper
    alias: str | None
    title: str
    year: int | None
    source_url: str | None
    licence: str | None
    access_date: str
    status: str
    text_path: str | None
    pdf_path: str | None
    sha256: str | None
    extractor: str | None
    run_id: str | None
    last_read: str | None = None
    code_available: str = "unknown"
    data_available: str = "unknown"
    note: str | None = None
    source_sha256: str | None = None  # sha256 of the file the text was extracted from (inbox files)
    extras_path: str | None = None  # tables, figure captions, formulas, kept apart from the main text (JATS only)
    extras_sha256: str | None = None
    refs_path: str | None = None  # the reference list, kept apart (JATS only)
    refs_sha256: str | None = None
    text_version: str | None = None  # the version of the paper the text comes from, e.g. "publishedVersion"
    resolver: dict[str, Any] | None = None  # the service that located the file and its version (OpenAlex fallback)

    def to_json(self) -> str:
        return json.dumps({**asdict(self), "aliases": list(self.aliases)}, sort_keys=True)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SourceRecord":
        known = {f for f in cls.__dataclass_fields__}
        row = {k: v for k, v in data.items() if k in known}
        row["aliases"] = tuple(row.get("aliases") or ())
        return cls(**row)


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _atomic_write(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class LiteratureStore:
    def __init__(self, root: Path | str = DEFAULT_ROOT, clock: Callable[[], str] = _now) -> None:
        self.root = Path(root)
        self._clock = clock

    # ---- paths -------------------------------------------------------------------------------------
    @property
    def index_path(self) -> Path:
        return self.root / "index.jsonl"

    def _relative(self, path: str | None) -> str | None:
        """A path as it should be stored: relative when it is inside this store, unchanged when it is not.

        `pdf_path` is the caller's file — `tools/literature/ingest.py` passes an inbox path — and the store
        does not own it, so a PDF outside the store keeps the spelling it was given. One that was placed
        inside the store is relativised like the texts, because it is then subject to the same move.
        """
        if not path:
            return path
        candidate = Path(path)
        if not candidate.is_absolute():
            return path
        try:
            return str(candidate.relative_to(self.root.resolve()))
        except ValueError:
            return path

    def resolve(self, path: str) -> Path:
        """A stored path as this store reads it.

        Paths are written **relative to the store root** (`text/<file>.txt`) so that moving the checkout, or
        the package inside it, cannot orphan a text that is sitting on disk. Older records may hold an
        absolute path and are still honoured as written; one built from a previous layout resolves nowhere and
        makes `check_quote` answer `text_hash_mismatch`. `relink_paths` rewrites those; this keeps the ones that
        still point somewhere real.
        """
        candidate = Path(path)
        return candidate if candidate.is_absolute() else self.root / candidate

    # ---- reading -----------------------------------------------------------------------------------
    def _all_records(self) -> list[SourceRecord]:
        if not self.index_path.is_file():
            return []
        out: list[SourceRecord] = []
        for number, line in enumerate(self.index_path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                out.append(SourceRecord.from_dict(json.loads(line)))
            except (json.JSONDecodeError, TypeError) as err:
                raise ValueError(f"{self.index_path}:{number}: unreadable index record: {err}") from err
        return out

    def records(self) -> Iterator[SourceRecord]:
        """The newest record of every paper, in first-added order."""
        latest: dict[str, SourceRecord] = {}
        for record in self._all_records():
            latest[record.canonical_id] = record
        yield from latest.values()

    def lookup(self, identifier: str, verify: bool = True) -> SourceRecord | None:
        """The newest record for any spelling or alias of the paper; None when unknown.

        With `verify` (default) a record whose text file is missing or no longer matches its recorded
        sha256 is treated as absent: a modified text must never be used silently as if it were the source.
        """
        wanted = canonical_id(identifier) or (identifier.strip().lower() if isinstance(identifier, str) else "")
        if not wanted:
            return None
        found: SourceRecord | None = None
        for record in self.records():
            keys = {record.canonical_id.lower(), *(a.lower() for a in record.aliases)}
            if record.alias:
                keys.add(record.alias.lower())
            if wanted.lower() in keys:
                found = record
        if found is None:
            return None
        if verify and found.text_path:
            path = self.resolve(found.text_path)
            if not path.is_file() or _sha256(path.read_bytes()) != found.sha256:
                return None
        return found

    # ---- writing -----------------------------------------------------------------------------------
    def _append(self, record: SourceRecord) -> None:
        with _locked(self.root / ".index.lock"):
            with open(self.index_path, "a", encoding="utf-8") as handle:
                handle.write(record.to_json() + "\n")
                handle.flush()
                os.fsync(handle.fileno())

    def _identity(self, identifiers: Iterable[str], alias: str | None) -> tuple[str, tuple[str, ...]]:
        canon = [c for c in (canonical_id(i) for i in identifiers) if c]
        if not canon:
            raise ValueError("a record needs at least one recognisable identifier (doi/pmid/pmcid)")
        aliases = list(dict.fromkeys(canon))
        return aliases[0], tuple(aliases)

    def add(
        self,
        *,
        identifiers: Iterable[str],
        title: str,
        year: int | None,
        text: str,
        source_url: str | None,
        licence: str | None,
        extractor: str | None,
        run_id: str | None,
        alias: str | None = None,
        pdf_path: str | None = None,
        code_available: str = "unknown",
        data_available: str = "unknown",
        note: str | None = None,
        source_sha256: str | None = None,
        extras: str | None = None,
        refs: str | None = None,
        text_version: str | None = None,
        resolver: dict[str, Any] | None = None,
    ) -> SourceRecord:
        """Store the extracted text of a paper and append its record (never overwrites an earlier one)."""
        for value in (code_available, data_available):
            if value not in AVAILABILITY:
                raise ValueError(f"availability must be one of {AVAILABILITY}, got {value!r}")
        primary, aliases = self._identity(identifiers, alias)
        body = text.encode("utf-8")
        digest = _sha256(body)
        status = "full_text" if len(text.strip()) >= MIN_FULL_TEXT_CHARS else "abstract_only"
        text_file = self.root / "text" / f"{_safe_name(primary)}-{digest[:12]}.txt"
        _atomic_write(text_file, text)  # content-addressed name: an older version stays readable
        companions: dict[str, str] = {}
        for kind, content in (("extras", extras), ("refs", refs)):
            if content:
                companion = text_file.with_suffix(f".{kind}.txt")
                _atomic_write(companion, content)
                companions[f"{kind}_path"] = str(companion.relative_to(self.root))
                companions[f"{kind}_sha256"] = _sha256(content.encode("utf-8"))
        record = SourceRecord(
            canonical_id=primary,
            aliases=aliases,
            alias=alias,
            title=title,
            year=year,
            source_url=source_url,
            licence=licence,
            access_date=self._clock(),
            status=status,
            text_path=str(text_file.relative_to(self.root)),
            pdf_path=self._relative(pdf_path),
            sha256=digest,
            extractor=extractor,
            run_id=run_id,
            last_read=self._clock(),
            code_available=code_available,
            data_available=data_available,
            note=note,
            source_sha256=source_sha256,
            text_version=text_version,
            resolver=resolver,
            **companions,
        )
        self._append(record)
        return record

    def companion(self, record: SourceRecord, kind: str) -> str | None:
        """The `extras` or `refs` text of a record; None when there is none or its sha256 no longer matches."""
        path, digest = getattr(record, f"{kind}_path"), getattr(record, f"{kind}_sha256")
        if not path or not self.resolve(path).is_file():
            return None
        data = self.resolve(path).read_bytes()
        return data.decode("utf-8") if _sha256(data) == digest else None

    PATH_FIELDS: Final = (("text_path", "sha256"), ("extras_path", "extras_sha256"), ("refs_path", "refs_sha256"))

    def integrity(self) -> dict[str, Any]:
        """Does every current record's text resolve, and does it still hash to what the record declares?

        The one check no stored-path regression can survive, and the only one that also catches a corruption unrelated to them. Reads the texts, so it is not free;
        it is meant to be run deliberately, not inside a phase.
        """
        ok: list[str] = []
        missing: list[dict[str, str]] = []
        mismatched: list[dict[str, str]] = []
        no_text: list[str] = []
        for record in self.records():
            if not record.text_path:
                no_text.append(record.canonical_id)
                continue
            path = self.resolve(record.text_path)
            if not path.is_file():
                missing.append({"paper": record.canonical_id, "path": record.text_path})
            elif record.sha256 and _sha256(path.read_bytes()) != record.sha256:
                mismatched.append({"paper": record.canonical_id, "path": record.text_path})
            else:
                ok.append(record.canonical_id)
        return {
            "papers": len(ok) + len(missing) + len(mismatched) + len(no_text),
            "readable": len(ok),
            "no_text_recorded": no_text,
            "missing": missing,
            "mismatched": mismatched,
            "sound": not missing and not mismatched,
        }

    def relink_paths(self, dry_run: bool = True) -> dict[str, Any]:
        """Re-point records whose text is on disk but whose stored path does not resolve, and relativise the
        rest, by **appending** corrected records.

        The index is append-only and `records()` keeps the newest record of each paper, so a stale path is
        corrected by appending a record that differs from it only in its path fields: nothing is rewritten,
        the earlier spelling stays readable, and a run that recorded the old index sha256 keeps meaning what
        it meant.

        A path is only corrected when the file it should name is present **and** hashes to the sha256 the
        record already declares. A file that does not match is left exactly as it is and reported under
        `mismatched`: re-pointing a record at a text that is not the one it was written from would be a worse
        fault than the broken path, because every later quote check would then pass against the wrong source.

        Returns what it did or would do, per paper, and never raises on a single bad record.
        """
        relinked: dict[str, dict[str, str]] = {}
        missing: list[dict[str, str]] = []
        mismatched: list[dict[str, str]] = []
        outside: list[dict[str, str]] = []
        for record in self.records():
            fields: dict[str, str] = {}
            for key, digest_key in self.PATH_FIELDS:
                stored = getattr(record, key)
                if not stored:
                    continue
                current = self.resolve(stored)
                chosen = current if current.is_file() else self.root / "text" / Path(stored).name
                if not chosen.is_file():
                    missing.append({"paper": record.canonical_id, "field": key, "path": stored})
                    continue
                digest = getattr(record, digest_key)
                if digest and _sha256(chosen.read_bytes()) != digest:
                    mismatched.append({"paper": record.canonical_id, "field": key, "path": stored})
                    continue
                try:
                    relative = str(chosen.relative_to(self.root))
                except ValueError:
                    outside.append({"paper": record.canonical_id, "field": key, "path": stored})
                    continue
                if relative != stored:
                    fields[key] = relative
            if fields:
                relinked[record.canonical_id] = fields
                if not dry_run:
                    self._append(replace(record, **fields))
        return {
            "dry_run": dry_run,
            "papers": len(list(self.records())),
            "relinked": relinked,
            "missing": missing,
            "mismatched": mismatched,
            "outside_store": outside,
        }

    def summary(self) -> dict[str, Any]:
        """Papers by status and the sha256 of the index file (None when there is no index): what a run
        records to say which store it read. Never reads the texts."""
        papers = {status: 0 for status in STATUSES}
        for record in self.records():
            papers[record.status] = papers.get(record.status, 0) + 1
        index = _sha256(self.index_path.read_bytes()) if self.index_path.is_file() else None
        return {"papers": papers, "index_sha256": index}

    def save_pdf(self, canonical: str, data: bytes) -> Path:
        """Write a downloaded PDF under `pdf/` with a content-addressed name; returns its absolute path."""
        path = self.root.resolve() / "pdf" / f"{_safe_name(canonical)}-{_sha256(data)[:12]}.pdf"
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return path

    def find_by_source_sha256(self, digest: str) -> SourceRecord | None:
        """The newest record made from a file with this sha256, or None (used to ingest a file only once)."""
        found = None
        for record in self._all_records():
            if record.source_sha256 == digest:
                found = record
        return found

    def add_unavailable(
        self,
        *,
        identifiers: Iterable[str],
        title: str,
        reason: str,
        run_id: str | None,
        source_url: str | None = None,
        licence: str | None = None,
    ) -> SourceRecord:
        """Remember that a paper is known but its text is not available (paywall, unresolvable, ...)."""
        primary, aliases = self._identity(identifiers, None)
        record = SourceRecord(
            canonical_id=primary,
            aliases=aliases,
            alias=None,
            title=title,
            year=None,
            source_url=source_url,
            licence=licence,
            access_date=self._clock(),
            status="not_available",
            text_path=None,
            pdf_path=None,
            sha256=None,
            extractor=None,
            run_id=run_id,
            note=reason,
        )
        self._append(record)
        return record

    # ---- views -------------------------------------------------------------------------------------
    def snapshot(self) -> list[dict[str, Any]]:
        """What a workflow needs to know about the store: ids, paths, hashes, status (never the texts)."""
        return [
            {
                "canonical_id": r.canonical_id,
                "alias": r.alias,
                "status": r.status,
                "text_path": r.text_path,
                "sha256": r.sha256,
                "licence": r.licence,
            }
            for r in self.records()
        ]

    def render_index_md(self) -> str:
        rows = [
            "| canonical id | alias | status | year | licence | title | access date |",
            "|---|---|---|---|---|---|---|",
        ]
        for r in self.records():
            cells = [
                r.canonical_id,
                r.alias or "",
                r.status,
                str(r.year or ""),
                r.licence or "",
                r.title.replace("|", "/"),
                r.access_date,
            ]
            rows.append("| " + " | ".join(cells) + " |")
        text = (
            "# Literature store index\n\nGenerated from `index.jsonl` by `LiteratureStore.render_index_md`; "
            "do not edit by hand.\n\n" + "\n".join(rows) + "\n"
        )
        _atomic_write(self.root / "INDEX.md", text)
        return text


def extract_pdf_text(pdf: Path, runner: Callable[..., Any] = subprocess.run) -> tuple[str, str]:
    """Extract text with `pdftotext`; returns (text, "pdftotext <version>"). Raises on failure, never returns ''."""
    try:
        done = runner(["pdftotext", str(pdf), "-"], capture_output=True, text=True, check=False)
    except FileNotFoundError as err:
        raise ExtractionError("pdftotext is not installed") from err
    if done.returncode != 0:
        raise ExtractionError(f"pdftotext failed on {pdf.name}: {(done.stderr or '').strip()}")
    try:
        version = runner(["pdftotext", "-v"], capture_output=True, text=True, check=False)
        line = ((version.stderr or version.stdout or "").strip().splitlines() or [""])[0]
    except FileNotFoundError:
        line = ""
    # The test runner returns the same stderr for both calls; a real one prints the version on stderr of -v.
    ident = line if "pdftotext" in line.lower() else f"pdftotext {line}".strip()
    return done.stdout, ident
