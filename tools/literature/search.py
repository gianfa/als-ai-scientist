"""Cache-first topical search over OpenAlex works.

`search_works` asks OpenAlex for the works closest to a free-text query and returns records built only from
fields the service returned. It is the search leg that `fetch.py` lacks: `fetch.py` resolves a paper whose
identifier is already known, this finds papers by topic.

What a result means, stated rather than hidden:
- A list of works is what OpenAlex's relevance ranking returned for this query on the access date. It is **not**
  the literature on the topic: coverage is OpenAlex's, abstracts may be missing, and the ranking can change
  between two calls. Absence from a result is therefore never evidence of absence from the literature.
- Every answer is stored in an append-only JSONL cache with its URL, access date and the API version OpenAlex
  declared at that time, so a run can be re-read without the network (`allow_network=False`) and a later change
  of the service is visible as a different record, not silently absorbed.
- Calls are keyless (OpenAlex's smaller anonymous daily budget). No credential is read or stored here.
- The cache is local data, never committed (same as the other `data/external/` caches).
"""

from __future__ import annotations

import json
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from tools.literature.fetch import (
    FetchError,
    Retrying,
    Transport,
    _openalex_url,
    http_transport,
    openalex_api_version,
)

DEFAULT_CACHE = Path("data/external/openalex-search-cache/search.jsonl")
DEFAULT_LIMIT = 10
MAX_LIMIT = 25
_SELECT = "id,doi,title,publication_year,cited_by_count,abstract_inverted_index"


class CacheMiss(RuntimeError):
    """A query is not cached and the network is not allowed."""


def _key(query: str, limit: int) -> str:
    return f"{' '.join(query.lower().split())}|{limit}"


def _url(query: str, limit: int) -> str:
    params = urllib.parse.urlencode({"search": query, "per-page": limit, "select": _SELECT})
    return _openalex_url(f"works?{params}")


def abstract_of(inverted: Any) -> str | None:
    """The abstract text rebuilt from OpenAlex's `{word: [positions]}` index; None when it carries none."""
    if not isinstance(inverted, dict) or not inverted:
        return None
    slots: dict[int, str] = {}
    for word, positions in inverted.items():
        if isinstance(word, str) and isinstance(positions, list):
            for pos in positions:
                if isinstance(pos, int):
                    slots[pos] = word
    return " ".join(slots[i] for i in sorted(slots)) or None


def _work(raw: dict[str, Any]) -> dict[str, Any]:
    doi = raw.get("doi")
    return {
        "openalex_id": raw.get("id"),
        "doi": doi[len("https://doi.org/") :] if isinstance(doi, str) and doi.startswith("https://doi.org/") else doi,
        "title": raw.get("title"),
        "year": raw.get("publication_year"),
        "cited_by_count": raw.get("cited_by_count"),
        "abstract": abstract_of(raw.get("abstract_inverted_index")),
    }


def _load_cache(path: Path) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    if path.is_file():
        for line in path.read_text().splitlines():
            if line.strip():
                rec = json.loads(line)
                records[rec["key"]] = rec  # later lines win
    return records


def _append(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def search_works(
    query: str,
    *,
    limit: int = DEFAULT_LIMIT,
    cache_path: Path | str = DEFAULT_CACHE,
    transport: Transport = http_transport,
    allow_network: bool = True,
    refresh: bool = False,
) -> dict[str, Any]:
    """The works OpenAlex ranks closest to `query`, as `{query, limit, works, retrieved_at, url, api_version,
    from_cache}`. Raises `ValueError` for an empty query or a limit outside 1..MAX_LIMIT, `CacheMiss` when the
    query is not cached and the network is forbidden, and `FetchError` when OpenAlex answers with something that
    is not the expected `{"results": [...]}` shape (a failure to retry, not an empty result). A 429 or 503 is retried
    first (`Retrying`); `FetchError` then means the refusal survived the retries.

    OpenAlex limits depend on the access tier, with a daily budget and a rate limit per hour: check the tier at
    https://help.openalex.org/access/example-costs/. A `Retry-After` of hours is a spent daily budget, which no
    retry can fix (observed 2026-10-14: 429 with `Retry-After` of 33993 s on a keyless call)."""
    if not query or not query.strip():
        raise ValueError("empty search query")
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_LIMIT}")
    path, key = Path(cache_path), _key(query, limit)
    cache = _load_cache(path)
    if key in cache and not refresh:
        return {**cache[key], "from_cache": True}
    if not allow_network:
        raise CacheMiss(f"{query!r} not cached")
    url = _url(query, limit)
    # A refused request (429, 503) is retried with the backoff `fetch` already applies; a transport the caller
    # wrapped itself is left alone, so the policy lives in `Retrying` and nowhere else.
    transport = transport if isinstance(transport, Retrying) else Retrying(transport)
    try:
        body = json.loads(transport(url))
        results = body["results"]
    except (json.JSONDecodeError, KeyError, TypeError) as err:
        raise FetchError(f"{url}: unexpected OpenAlex response: {err}") from err
    except OSError as err:
        raise FetchError(f"{url}: {err}") from err
    if not isinstance(results, list):
        raise FetchError(f"{url}: `results` is not a list")
    record = {
        "key": key,
        "query": query,
        "limit": limit,
        "works": [_work(r) for r in results if isinstance(r, dict)],
        "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "url": url,
        "api_version": openalex_api_version(transport),
    }
    _append(path, record)
    return {**record, "from_cache": False}


__all__: Iterable[str] = ["CacheMiss", "DEFAULT_CACHE", "abstract_of", "search_works"]
