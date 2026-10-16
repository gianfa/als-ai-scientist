"""Cache-first client for MyChem.info's drug/chemical annotation API.

`lookup_drug` queries MyChem.info's `/v1/query` endpoint by name and returns a record built only from
fields the service actually returned. MyChem.info aggregates DrugBank, ChEMBL, ChEBI, PubChem and UNII
(the same aggregator biomcp's `get_drug` wraps via `biothings_client` -- rejected as a dependency).
Every lookup is stored in an append-only JSONL
cache and later calls for the same name are served from it; the network is only touched for a name not
yet cached and can be forbidden altogether (`allow_network=False`).

Limits, stated rather than hidden:
- Unlike `netmhciipan.py`'s IEDB grid, the set of drug names this client is asked about is
  NOT known in advance: it is whatever `intervene` proposes, each run. A genuinely new name can
  therefore require a real HTTP call during a production run; this client cannot guarantee zero network
  use the way `netmhciipan.py` does once its grid is pre-acquired. That is a declared deviation from
  that shape, not a flaw in this file.
- A "found" record means this code queried MyChem.info and got back a row whose own display name
  matches the query; it does not mean MyChem.info's underlying data is frozen, versioned or
  independently audited. The response carries no tool/dataset version; a service-side update would not
  be detected, and a later query of the same name may return a different record.
- Matching is deliberately conservative and never guesses: a name resolves to "found" only when exactly
  one hit's own display name (drugbank.name / chembl.pref_name / chebi.name / unii.display_name)
  case-insensitively equals the query, or there is exactly one hit at all. Verified live (2026-10-09):
  this means a query by brand/common synonym that the aggregator does not index under any of those four
  fields (e.g. "rapamycin", whose records are filed under "sirolimus") is reported as "not found", even
  though a human reader would recognise the single highest-scored hit as the right one. This is a
  deliberate false negative, not a wrong match: the alternative (picking the top-scored hit) risks a
  silently wrong drug identity, which this project's scientific-integrity rule forbids more strongly
  than it forbids an occasional unresolved name.
- Cache files are local-only data, never committed (same as `netmhciipan.py`).
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

API_URL = "https://mychem.info/v1/query"
DEFAULT_CACHE = Path("data/external/mychem-cache/pharmacology.jsonl")
_FIELDS = (
    "drugbank.id",
    "drugbank.name",
    "chembl.molecule_chembl_id",
    "chembl.pref_name",
    "chebi.id",
    "chebi.name",
    "pubchem.cid",
    "unii.unii",
    "unii.display_name",
)

Transport = Callable[[str], str]


class CacheMiss(RuntimeError):
    """A name is not cached and the network is not allowed."""


class MalformedResponse(ValueError):
    """The service answered, but not with the expected `{"total": ..., "hits": [...]}` shape."""


@dataclass(frozen=True)
class DrugRecord:
    query: str
    drugbank_id: str | None
    chembl_id: str | None
    chebi_id: str | None
    pubchem_cid: str | None
    unii: str | None
    tradename: str | None
    retrieved_at: str
    source_url: str


def http_transport(url: str, timeout: float = 30.0) -> str:
    """GET the URL and return the response body (raises on HTTP errors)."""
    with urllib.request.urlopen(urllib.request.Request(url), timeout=timeout) as resp:
        return resp.read().decode()


def _key(name: str) -> str:
    return name.strip().lower()


def _url(name: str) -> str:
    q = urllib.parse.quote(name.strip())
    fields = ",".join(_FIELDS)
    return f"{API_URL}?q={q}&fields={fields}&size=5&dotfield=true"


def _load_cache(path: Path) -> dict[str, dict]:
    records: dict[str, dict] = {}
    if path.is_file():
        for line in path.read_text().splitlines():
            if line.strip():
                rec = json.loads(line)
                records[rec["key"]] = rec  # later lines win: newest record is used
    return records


def _append(path: Path, records: Iterable[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        for rec in records:
            handle.write(json.dumps(rec, sort_keys=True) + "\n")


def _display_names(hit: dict[str, Any]) -> set[str]:
    names = (hit.get("drugbank.name"), hit.get("chembl.pref_name"), hit.get("chebi.name"), hit.get("unii.display_name"))
    return {n.strip().lower() for n in names if isinstance(n, str) and n.strip()}


def _parse(text: str, name: str) -> dict[str, Any]:
    """Return `{"match": "found"|"none"|"ambiguous", "hit": {...} | None}`. A zero-hit answer is a
    confirmed, valid "none", not malformed. `MalformedResponse` is raised only when the body is not even
    the expected biothings shape (e.g. an HTML error page, a rate-limit message)."""
    try:
        body = json.loads(text)
    except json.JSONDecodeError as err:
        raise MalformedResponse(f"not JSON: {text[:120]!r}") from err
    if not isinstance(body, dict) or "hits" not in body or "total" not in body:
        raise MalformedResponse(f"unexpected response shape: {text[:120]!r}")
    hits = body["hits"]
    if body["total"] == 0 or not hits:
        return {"match": "none", "hit": None}
    target = name.strip().lower()
    exact = [h for h in hits if target in _display_names(h)]
    if len(exact) == 1:
        return {"match": "found", "hit": exact[0]}
    if len(exact) > 1:
        return {"match": "ambiguous", "hit": None}
    if len(hits) == 1:
        return {"match": "found", "hit": hits[0]}
    return {"match": "ambiguous", "hit": None}


def _first(value: Any) -> str | None:
    """An identifier field as text. A list (MyChem.info returns several PubChem CIDs for metformin, the parent
    compound not first) is kept whole, comma-separated: choosing one by position would invent an identity."""
    if isinstance(value, list):
        return ", ".join(dict.fromkeys(str(v) for v in value)) or None
    return str(value) if value is not None else None


def _record_from_hit(hit: dict[str, Any], query: str, retrieved_at: str, source_url: str) -> DrugRecord:
    drugbank_id, chembl_id, chebi_id = (
        _first(hit.get("drugbank.id")),
        _first(hit.get("chembl.molecule_chembl_id")),
        _first(hit.get("chebi.id")),
    )
    tradename = next(
        (
            n
            for n in (
                hit.get("drugbank.name"),
                hit.get("chembl.pref_name"),
                hit.get("chebi.name"),
                hit.get("unii.display_name"),
            )
            if n
        ),
        None,
    )
    return DrugRecord(
        query=query,
        drugbank_id=drugbank_id,
        chembl_id=chembl_id,
        chebi_id=chebi_id,
        pubchem_cid=_first(hit.get("pubchem.cid")),
        unii=_first(hit.get("unii.unii")),
        tradename=tradename,
        retrieved_at=retrieved_at,
        source_url=source_url,
    )


def _cached_or_fetch(
    name: str, cache_path: Path | str, transport: Transport, allow_network: bool, refresh: bool
) -> dict[str, Any]:
    if not name or not name.strip():
        raise ValueError("empty drug name")

    cache_path = Path(cache_path)
    cache = _load_cache(cache_path)
    key = _key(name)

    if refresh or key not in cache:
        if not allow_network:
            raise CacheMiss(f"{name!r} not cached")
        url = _url(name)
        parsed = _parse(transport(url), name)
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        rec = {
            "key": key,
            "request": {"name": name},
            "match": parsed["match"],
            "hit": parsed["hit"],
            "retrieved_at": stamp,
            "url": url,
        }
        cache[key] = rec
        _append(cache_path, [rec])

    return cache[key]


def lookup_match(
    name: str,
    *,
    cache_path: Path | str = DEFAULT_CACHE,
    transport: Transport = http_transport,
    allow_network: bool = True,
    refresh: bool = False,
) -> tuple[str, DrugRecord | None, str]:
    """Like `lookup_drug`, but also says which kind of miss it was: returns `(match, record, source_url)` with
    `match` one of "found" / "none" / "ambiguous" (`record` is set only for "found"). Same raises as `lookup_drug`."""
    rec = _cached_or_fetch(name, cache_path, transport, allow_network, refresh)
    record = _record_from_hit(rec["hit"], name, rec["retrieved_at"], rec["url"]) if rec["match"] == "found" else None
    return rec["match"], record, rec["url"]


def lookup_drug(
    name: str,
    *,
    cache_path: Path | str = DEFAULT_CACHE,
    transport: Transport = http_transport,
    allow_network: bool = True,
    refresh: bool = False,
) -> DrugRecord | None:
    """Look up `name` against MyChem.info. `None` means a confirmed "no match" or a confirmed "several
    candidates, no single unambiguous one" -- this client never guesses between them; both are recorded
    distinctly in the cache (`match`: "none" vs "ambiguous") even though both return `None` here. Raises
    `CacheMiss` if the name is not cached and `allow_network=False`. Raises `MalformedResponse` if the
    service answers with something that is not even the expected shape; a confirmed miss is cached, so
    the same absent name is not re-queried on a later call."""
    match, record, _ = lookup_match(
        name, cache_path=cache_path, transport=transport, allow_network=allow_network, refresh=refresh
    )
    return record


__all__ = ["CacheMiss", "MalformedResponse", "DrugRecord", "lookup_drug", "lookup_match"]
