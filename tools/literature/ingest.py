"""Bring the files a user dropped in `literature/inbox/` into the store (deterministic; no LLM, no agent).

For each file (`.pdf`, `.txt`, JATS `.xml`) the function decides one outcome:

- `ingested`            text extracted and registered under an identifier;
- `already_ingested`    a file with the same sha256 is already in the store (nothing is done twice);
- `pending_identifier`  no identifier could be established; nothing is registered and the reason is reported;
- `failed_extraction`   the text could not be extracted;
- `unsupported_type`    not a file type this step reads.
A row of the sources file whose file is not in the inbox is reported as `missing_file`.

Where the identifier comes from, in order (an identifier is the anchor of every citation, so it is never guessed):
1. the user's sources file (`literature/sources.csv`: `file,identifier,title,year,alias,note`): a row wins;
2. a DOI found in the first part of the text, accepted only if Europe PMC resolves it AND the title Europe PMC
   gives for it occurs in that text, compared on letters and digits only (PDF text splits words: the real
   Michaelis PDF reads "C9orf 72"); recorded as inferred;
3. otherwise the file is pending: add a row to the sources file.
An agent may one day PROPOSE a row; it never writes the index (see docs/tools/literature.md).

The user's files are never moved, renamed or deleted. Network access (step 2 only) goes through an injected
`Transport`; offline, step 2 simply yields `pending_identifier`.
"""

from __future__ import annotations

import csv
import hashlib
import re
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from tools.literature.fetch import EXTRACTOR as JATS_EXTRACTOR
from tools.literature.fetch import FetchError, PdfExtractor, Transport, jats_parts, make_alias, resolve
from tools.literature.store import REPO_ROOT, ExtractionError, LiteratureStore, canonical_id, extract_pdf_text

DEFAULT_INBOX = REPO_ROOT / "literature" / "inbox"
DEFAULT_SOURCES_CSV = REPO_ROOT / "literature" / "sources.csv"
OUTCOMES = (
    "ingested",
    "already_ingested",
    "pending_identifier",
    "failed_extraction",
    "unsupported_type",
    "missing_file",
)
FIRST_PART_CHARS = 6000  # where the title and DOI of a paper are looked for
MAX_DOI_CANDIDATES = 3
MIN_TITLE_CHARS = 25  # letters and digits: a shorter title would match almost any text
_DOI = re.compile(r"\b10\.\d{4,9}/[^\s\"<>]+", re.IGNORECASE)


@dataclass(frozen=True)
class IngestResult:
    file: str
    outcome: str
    canonical_id: str | None = None
    identifier_source: str | None = None  # sources_csv | inferred_doi_title_match
    detail: str = ""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_sources(csv_path: Path | None) -> dict[str, dict[str, str]]:
    """The user's sources file as {file name: row}. Missing file = no rows; a row without `file` is an error."""
    if csv_path is None or not csv_path.is_file():
        return {}
    rows: dict[str, dict[str, str]] = {}
    with open(csv_path, newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or "file" not in reader.fieldnames:
            raise ValueError(f"{csv_path}: the header must contain a `file` column")
        for number, row in enumerate(reader, start=2):
            clean = {k.strip(): (v or "").strip() for k, v in row.items() if k}
            if not clean.get("file"):
                if any(clean.values()):
                    raise ValueError(f"{csv_path}:{number}: row without a `file`")
                continue
            rows[clean["file"]] = clean
    return rows


def _extract(path: Path, extract_pdf: PdfExtractor) -> tuple[str, str, str, str]:
    """(main text, extractor, extras, refs); extras and refs are empty except for JATS."""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return (*extract_pdf(path), "", "")
    if suffix == ".txt":
        return path.read_text(encoding="utf-8"), "plain-text", "", ""
    parts = jats_parts(path.read_text(encoding="utf-8"))  # .xml
    return parts.main, JATS_EXTRACTOR, parts.extras, parts.refs


def _squash(text: str) -> str:
    """Lower case letters and digits only: no spaces, no punctuation (NFKC first)."""
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", text).lower())


def _title_in(title: str, text: str) -> bool:
    squashed = _squash(title)
    return len(squashed) >= MIN_TITLE_CHARS and squashed in _squash(text)


def _infer(text: str, transport: Transport) -> tuple[dict[str, Any] | None, str]:
    """A Europe PMC record whose DOI is in the first part of the text and whose title is there too."""
    first = text[:FIRST_PART_CHARS]
    candidates: list[str] = []
    for match in _DOI.findall(first):
        canonical = canonical_id(match)
        if canonical and canonical not in candidates:
            candidates.append(canonical)
    if not candidates:
        return None, "no DOI found in the first part of the text"
    why = "no candidate DOI matched"
    for canonical in candidates[:MAX_DOI_CANDIDATES]:
        try:
            record = resolve(canonical, transport)
        except (FetchError, OSError) as err:
            return None, f"could not ask Europe PMC about {canonical}: {err}"
        if record is None:
            why = f"{canonical} is unknown to Europe PMC"
            continue
        title = str(record.get("title") or "")
        if _title_in(title, first):
            return record, f"DOI {canonical} resolved on Europe PMC and its title is in the text"
        why = f"the Europe PMC title of {canonical} does not occur in the text"
    return None, why


def _ids_from_record(record: Mapping[str, Any]) -> list[str]:
    return [
        f"{kind}:{record[field]}"
        for field, kind in (("doi", "doi"), ("pmid", "pmid"), ("pmcid", "pmcid"))
        if record.get(field)
    ]


def ingest_inbox(
    inbox: Path,
    sources_csv: Path | None,
    store: LiteratureStore,
    transport: Transport,
    *,
    run_id: str | None = None,
    extract_pdf: PdfExtractor = extract_pdf_text,
) -> list[IngestResult]:
    """Ingest every supported file of `inbox`, in name order; one result per file (plus `missing_file` rows)."""
    sources = read_sources(sources_csv)
    results: list[IngestResult] = []
    present: set[str] = set()
    files = sorted(p for p in inbox.iterdir() if p.is_file() and not p.name.startswith(".")) if inbox.is_dir() else []
    for path in files:
        present.add(path.name)
        if path.suffix.lower() not in (".pdf", ".txt", ".xml"):
            results.append(IngestResult(path.name, "unsupported_type", detail="only .pdf, .txt and JATS .xml are read"))
            continue
        digest = _sha256_file(path)
        earlier = store.find_by_source_sha256(digest)
        if earlier is not None:
            results.append(IngestResult(path.name, "already_ingested", earlier.canonical_id))
            continue
        try:
            text, extractor, extras, refs = _extract(path, extract_pdf)
        except (ExtractionError, FetchError, OSError, UnicodeDecodeError) as err:
            results.append(IngestResult(path.name, "failed_extraction", detail=str(err)))
            continue
        if not text.strip():
            results.append(
                IngestResult(path.name, "failed_extraction", detail="the file has no extractable text (scanned?)")
            )
            continue
        row = sources.get(path.name, {})
        if row.get("identifier"):
            ident_source, ids = "sources_csv", [row["identifier"]]
            if canonical_id(row["identifier"]) is None:
                results.append(
                    IngestResult(
                        path.name,
                        "pending_identifier",
                        detail=f"identifier {row['identifier']!r} in the sources file is not a DOI, PMID or PMCID",
                    )
                )
                continue
            licence = None
            title, year, alias = (
                row.get("title") or path.stem,
                int(row["year"]) if row.get("year", "").isdigit() else None,
                row.get("alias") or None,
            )
            note = "identifier given in the sources file" + (f"; {row['note']}" if row.get("note") else "")
        else:
            record, why = _infer(text, transport)
            if record is None:
                results.append(
                    IngestResult(
                        path.name, "pending_identifier", detail=f"{why}; add a row for this file to the sources file"
                    )
                )
                continue
            ident_source, ids = "inferred_doi_title_match", _ids_from_record(record)
            title = str(record.get("title") or path.stem)
            year = int(record["pubYear"]) if str(record.get("pubYear") or "").isdigit() else None
            alias = row.get("alias") or make_alias(record)
            licence = str(record["license"]) if record.get("license") else None  # as Europe PMC reports it
            note = f"identifier inferred: {why}"
        stored = store.add(
            identifiers=ids,
            title=title,
            year=year,
            text=text,
            source_url=None,
            licence=licence,
            extractor=extractor,
            run_id=run_id,
            alias=alias,
            pdf_path=str(path),
            note=note,
            source_sha256=digest,
            extras=extras or None,
            refs=refs or None,
        )
        results.append(IngestResult(path.name, "ingested", stored.canonical_id, ident_source, stored.status))
    for name in sources:
        if name not in present:
            results.append(IngestResult(name, "missing_file", detail="listed in the sources file but not in the inbox"))
    return results


def report(results: Iterable[IngestResult]) -> dict[str, Any]:
    rows = [asdict(r) for r in results]
    return {"counts": {name: sum(1 for r in rows if r["outcome"] == name) for name in OUTCOMES}, "entries": rows}
