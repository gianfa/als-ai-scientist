"""MCP server (stdio) over the literature store: `check_quote`, and — when the phase has a budget —
`fetch_paper`, which brings a paper the agent has identified into the store so its quote can be checked.

Why a server: the workflow script cannot hold the paper texts (script size limit) and agents have no shell,
so the deterministic check is offered to them as a tool. Design rules:

- Errors are data. A tool never raises: a check that could not be made returns `status: "unverifiable"`
  with a `cause`, so "could not check" is never confused with "checked and absent". A server that is down
  cannot answer at all; the rule for callers is that no receipt means not verified.
- Fail closed: if the audit line cannot be written, the verdict is withheld (`service_error`).
- Decisions come from `tools.literature.quotes.classify_quote`; this module only looks up the paper,
  calls it, and reports. The `mcp` SDK is imported only in `build_server`, so the core functions are testable
  without it.
- `check_quote` is read-only on the store; its only write is one append-only JSONL audit line per call
  (`--log`). `fetch_paper` writes to the store, which is why it exists only where a budget grants it
  (`--max-fetch`, default 0, and the tool is then not registered at all). **The agent still locates and the
  code still decides**: the agent supplies one identifier and nothing else — no title,
  URL, text, alias or path — `tools.literature.store.canonical_id` resolves it to a DOI/PMID/PMCID or the
  call is refused, and `tools.literature.fetch.fetch_reading_list` decides what is stored, from Europe PMC
  and OpenAlex only, recording licence, extractor, version and hash. No LLM is in that path.

`read_paper` serves a stored full text in bounded pages (`--max-read`, the number of different papers it may open). `search_works`, granted the same way (`--max-search`, default 0, not registered at all), finds papers by topic on
OpenAlex through `tools.literature.search`: it reads nothing from the store and writes nothing to it, and returns
works, never a verdict — what a list of works means is the caller's to say, and absence from it is not absence
from the literature.

Run (from the repository root): `uv run python -m tools.literature.mcp_server --store DIR --log FILE
[--max-fetch N --max-search N --max-read N --run-id ID]` (add `--check` to only verify it can start).
Diagnostics go to stderr; stdout belongs to the protocol.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Final

from tools.literature.fetch import EXTRACTOR as JATS_EXTRACTOR
from tools.literature.fetch import (
    OUTCOMES,
    BytesTransport,
    FetchError,
    Transport,
    fetch_reading_list,
    http_bytes_transport,
    http_transport,
)
from tools.literature.quotes import NEAR_MIN_SCORE, assess_quote
from tools.literature.search import DEFAULT_CACHE as SEARCH_CACHE
from tools.literature.search import CacheMiss, search_works
from tools.literature.store import LiteratureStore, canonical_id

TOOL_VERSION: Final = "2"
MAX_QUOTE_CHARS: Final = 10_000
MAX_PASSAGE_CHARS: Final = 3_000
CAUSES: Final = (
    "bad_input",
    "paper_not_in_store",
    "text_not_available",
    "text_incomplete",
    "text_hash_mismatch",
    "service_error",
)

TOOL_DESCRIPTION: Final = (
    "Check whether a quote occurs in the stored full text of a paper. Arguments: `paper` (an alias such as "
    "'michaelis-2025-autoimmune', or a DOI/PMID/PMCID) and `quote` (one contiguous passage, at least 5 words, "
    "no '...'). Punctuation and letter case are ignored. `status` is 'checked' or 'unverifiable'. When "
    "checked, `verdict` is 'exact' (found: only this counts as verified), 'near' (not found as written; the "
    "returned `passage` is the closest stretch of the paper: re-read it and quote it literally, then call "
    "again) or 'absent' (no such passage). `critical_mismatch` true means a number, sign or negation of your "
    "quote is missing from that stretch. `assessment` says what a non-exact result means: 'in_paper_outside_main_text' (the quote is in a table, caption or formula of the paper, which the main text lacks: the quote is real but this tool cannot confirm it as written), 'not_literal' (close to a passage, not as written), 'not_in_stored_text' (nowhere in a text that covers the whole paper: the quote is not in the paper) or 'not_found_text_may_be_incomplete' (nowhere, but the stored text is known to be incomplete, e.g. from a PDF: this does not show the quote is invented). When 'unverifiable' the check could not be made (see `cause` and "
    "`detail`): this is NOT evidence that the quote is wrong or right. Keep `receipt_id`: it identifies the check."
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _unverifiable(cause: str, detail: str, paper: str | None = None) -> dict[str, Any]:
    return {
        "status": "unverifiable",
        "verdict": None,
        "score": None,
        "critical_mismatch": None,
        "passage": None,
        "located_in": None,
        "assessment": None,
        "text_completeness": None,
        "cause": cause,
        "detail": detail,
        "paper": paper,
        "text_sha256": None,
        "receipt_id": None,
        "tool_version": TOOL_VERSION,
    }


def _append_log(log_path: Path, entry: dict[str, Any]) -> None:
    """One `os.write` on an O_APPEND descriptor: concurrent callers do not interleave inside a line."""
    line = (json.dumps(entry, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    fd = os.open(log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)


def _check(store: LiteratureStore, paper: Any, quote: Any) -> dict[str, Any]:
    if not isinstance(paper, str) or not paper.strip():
        return _unverifiable("bad_input", "`paper` must be a non-empty string")
    if not isinstance(quote, str) or not quote.strip():
        return _unverifiable("bad_input", "`quote` must be a non-empty string", paper)
    if len(quote) > MAX_QUOTE_CHARS:
        return _unverifiable(
            "bad_input", f"`quote` is longer than {MAX_QUOTE_CHARS} characters: quote one passage", paper
        )
    record = store.lookup(paper)
    if record is None:
        if store.lookup(paper, verify=False) is None:
            return _unverifiable("paper_not_in_store", f"no paper {paper!r} in the literature store")
        return _unverifiable(
            "text_hash_mismatch", "the stored text is missing or does not match its recorded sha256", paper
        )
    if record.status == "not_available" or not record.text_path:
        return _unverifiable(
            "text_not_available",
            f"the text of {record.canonical_id} is not available ({record.note or 'no reason recorded'})",
            record.canonical_id,
        )
    text = store.resolve(record.text_path).read_text(encoding="utf-8")
    extras, refs = store.companion(record, "extras") or "", store.companion(record, "refs") or ""
    completeness = "full_jats" if record.extractor == JATS_EXTRACTOR else "partial" if record.extractor else "unknown"
    assessed = assess_quote(quote, text, extras, refs, completeness=completeness)
    result = assessed.check
    if record.status == "abstract_only" and result.verdict != "exact":
        return _unverifiable(
            "text_incomplete",
            "only the abstract of this paper is stored, so a missing quote proves nothing",
            record.canonical_id,
        )
    receipt = hashlib.sha256(f"{TOOL_VERSION}\n{record.sha256}\n{quote}\n{result.verdict}".encode("utf-8")).hexdigest()[
        :16
    ]
    return {
        "status": "checked",
        "verdict": result.verdict,
        "score": round(result.score, 4),
        "critical_mismatch": result.critical_mismatch,
        "passage": (result.passage or "")[:MAX_PASSAGE_CHARS] or None,
        "located_in": assessed.located_in,
        "assessment": assessed.assessment,
        "text_completeness": assessed.text_completeness,
        "cause": None,
        "detail": result.reason,
        "paper": record.canonical_id,
        "text_sha256": record.sha256,
        "receipt_id": receipt,
        "tool_version": TOOL_VERSION,
    }


def check_quote(
    store: LiteratureStore, log_path: Path, paper: Any, quote: Any, clock: Callable[[], str] = _utc_now
) -> dict[str, Any]:
    """The tool body. Always returns a result dict; every call, answered or not, leaves one audit line."""
    try:
        result = _check(store, paper, quote)
    except Exception as err:  # noqa: BLE001 - the boundary: anything unexpected becomes data, never a crash
        result = _unverifiable(
            "service_error", f"{type(err).__name__}: {err}", paper if isinstance(paper, str) else None
        )
    entry = {
        "ts": clock(),
        "paper_arg": paper if isinstance(paper, str) else repr(paper),
        "quote": quote if isinstance(quote, str) else repr(quote),
        "status": result["status"],
        "verdict": result["verdict"],
        "assessment": result["assessment"],
        "cause": result["cause"],
        "paper": result["paper"],
        "text_sha256": result["text_sha256"],
        "receipt_id": result["receipt_id"],
        "tool_version": TOOL_VERSION,
        "near_min": NEAR_MIN_SCORE,
    }
    try:
        _append_log(log_path, entry)
    except OSError as err:
        return _unverifiable(
            "service_error", f"the audit log cannot be written, so no verdict is given ({err})", result["paper"]
        )
    return result


# `fetch_paper`'s own three outcomes, then `fetch.OUTCOMES` verbatim. One vocabulary for one fact: the words an
# agent sees here are the words the host's own fetch records in the phase report, so `evaluation/` reads one set.
FETCH_OUTCOMES: Final = ("bad_input", "already_in_store", "budget_spent", *OUTCOMES)

FETCH_TOOL_DESCRIPTION: Final = (
    "Bring a paper you have identified into the literature store, so `check_quote` can check a quote against "
    "it. One argument: `identifier`, a DOI, PMID or PMCID and nothing else — a title, a URL or a sentence is "
    "refused, because the paper is fetched from Europe PMC and OpenAlex by that identifier. `outcome` is "
    "'fetched' (it is now in the store: call `check_quote` next), 'already_in_store' (it was there: check the "
    "quote), 'cached' (likewise), 'paywalled' (neither Europe PMC nor OpenAlex can give the full text; say so "
    "in your claim rather than dropping it), 'known_unavailable' (already established as unavailable), "
    "'unresolvable' (no such paper, or the identifier is not one), 'failed' (a transient error), "
    "'budget_spent' (this phase's fetch budget is used up) or 'bad_input'. `budget_left` says how many fetches "
    "remain and `retries` how many times a refused request had to be repeated. Call it once per identifier: asking twice for the same paper returns the same answer and buys "
    "nothing."
)


@dataclass
class FetchBudget:
    """How many papers this stage may still bring in. Spent only by a paper actually stored.

    Mirrors `fetch_reading_list`'s own rule that `max_new` is consumed only by a store write: a refusal, a
    paywall or a paper already held costs nothing, so an agent is never punished for asking.
    """

    left: int
    spent: int = 0

    def take(self) -> None:
        self.left = max(self.left - 1, 0)
        self.spent += 1


def _fetch_result(
    outcome: str, detail: str, budget: FetchBudget, identifier: Any, canonical: str | None = None, retries: int = 0
) -> dict[str, Any]:
    return {
        "outcome": outcome,
        "detail": detail,
        "paper": canonical,
        "identifier": identifier if isinstance(identifier, str) else repr(identifier),
        "budget_left": budget.left,
        "retries": retries,
        "tool_version": TOOL_VERSION,
    }


def fetch_paper(
    store: LiteratureStore,
    log_path: Path,
    identifier: Any,
    budget: FetchBudget,
    *,
    run_id: str | None = None,
    transport: Transport = http_transport,
    pdf_transport: BytesTransport | None = http_bytes_transport,
    clock: Callable[[], str] = _utc_now,
) -> dict[str, Any]:
    """The tool body. Always returns a result dict; every call, served or not, leaves one audit line.

    This exists because of a measurement: over 14 archives, 192 of the agents' 760
    `check_quote` calls were refused `paper_not_in_store` **on a resolvable DOI, PMID or PMCID** — 151 distinct
    papers the agents had correctly identified and could not read. 85 of them never reached a claim at all, so nothing in the
    archive recorded that the evidence had been found; 29 arrived in the store later in the same run, the agent
    having asked too early; and 16 were genuinely paywalled, which is the one part of the loss this tool cannot
    close.

    `LiteratureStore.lookup` re-reads the index on every call, so a paper fetched here is checkable by the next
    `check_quote` **of the agent that awaited this answer**. Calls are served concurrently, so another agent's
    check may still be answered while this fetch is in flight; it then reads `paper_not_in_store` as before,
    which is early rather than wrong.
    """
    try:
        if not isinstance(identifier, str) or not identifier.strip():
            result = _fetch_result("bad_input", "`identifier` must be a non-empty string", budget, identifier)
        elif (canonical := canonical_id(identifier)) is None:
            result = _fetch_result(
                "bad_input",
                "not a DOI, PMID or PMCID; those are the only forms a paper can " "be fetched by",
                budget,
                identifier,
            )
        # `verify=True`, the same first check `fetch_reading_list` makes: a record whose text file is missing or
        # no longer matches its sha256 reads as absent here too, so it is re-fetched rather than reported as
        # held while `check_quote` refuses it with `text_hash_mismatch`.
        elif (held := store.lookup(canonical)) is not None and held.status != "not_available":
            result = _fetch_result("already_in_store", held.status, budget, identifier, held.canonical_id)
        elif budget.left <= 0:
            result = _fetch_result(
                "budget_spent", f"this phase has already fetched {budget.spent} papers", budget, identifier, canonical
            )
        else:
            (entry,) = fetch_reading_list(
                [canonical], store, transport, max_new=1, run_id=run_id, pdf_transport=pdf_transport
            )
            if entry.outcome == "fetched":
                budget.take()
            result = _fetch_result(
                entry.outcome, entry.detail, budget, identifier, entry.canonical_id or canonical, retries=entry.retries
            )
    except Exception as err:  # noqa: BLE001 - the boundary: anything unexpected becomes data, never a crash
        result = _fetch_result("failed", f"{type(err).__name__}: {err}", budget, identifier)
    entry_log = {
        "ts": clock(),
        "event": "fetch",
        "identifier": result["identifier"],
        "paper": result["paper"],
        "outcome": result["outcome"],
        "detail": result["detail"],
        "budget_left": result["budget_left"],
        "retries": result["retries"],
        "run_id": run_id,
        "tool_version": TOOL_VERSION,
    }
    try:
        _append_log(log_path, entry_log)
    except OSError as err:
        # Fail closed, as `check_quote` does — but the side effect may already have happened, so the real
        # outcome is stated rather than erased: an unaudited fetch is not to be relied on, not pretended away.
        return _fetch_result(
            "failed",
            f"the audit log cannot be written, so this call is not on record "
            f"({err}); the fetch itself reported {result['outcome']}",
            budget,
            identifier,
            result["paper"],
            retries=result["retries"],
        )
    return result


SEARCH_TOOL_DESCRIPTION: Final = (
    "Search OpenAlex for works close to a free-text query. Argument: `query` (a few words naming the finding or "
    "mechanism, not a sentence). Returns up to 10 `works` (openalex_id, doi, title, year, cited_by_count, a "
    "shortened `abstract` or null), the `url`, `retrieved_at` and `api_version`. It is a relevance ranking over "
    "OpenAlex only: not the literature, and a work missing from it proves nothing about the literature. "
    "`outcome` is 'searched', 'bad_input', 'budget_spent' or 'failed'. Read the abstracts yourself; this tool "
    "does not say whether a work anticipates anything."
)
MAX_ABSTRACT_CHARS: Final = 600


@dataclass
class SearchBudget:
    """How many searches this stage may still make on the network. A repeat served from the cache costs nothing."""

    left: int
    spent: int = 0

    def take(self) -> None:
        self.left = max(self.left - 1, 0)
        self.spent += 1


def _search_result(
    outcome: str, detail: str, budget: SearchBudget, query: Any, works: list[dict[str, Any]] | None = None, **extra: Any
) -> dict[str, Any]:
    return {
        "outcome": outcome,
        "detail": detail,
        "query": query if isinstance(query, str) else repr(query),
        "works": works or [],
        "budget_left": budget.left,
        "tool_version": TOOL_VERSION,
        **extra,
    }


def search_literature(
    log_path: Path,
    query: Any,
    budget: SearchBudget,
    *,
    run_id: str | None = None,
    cache_path: Path = SEARCH_CACHE,
    transport: Transport = http_transport,
    clock: Callable[[], str] = _utc_now,
) -> dict[str, Any]:
    """The tool body. Always returns a result dict; every call leaves one audit line, with the query.

    The budget is spent only by a call that reaches OpenAlex. Whether a query was already cached is looked up
    first with the network forbidden, so a repeat is free and an exhausted budget still serves the cache.
    """
    try:
        if not isinstance(query, str) or not query.strip():
            result = _search_result("bad_input", "`query` must be a non-empty string", budget, query)
        else:
            try:
                got = search_works(query, cache_path=cache_path, transport=transport, allow_network=False)
            except CacheMiss:
                got = None
            if got is None and budget.left <= 0:
                result = _search_result(
                    "budget_spent", f"this phase has already made {budget.spent} searches", budget, query
                )
            else:
                if got is None:
                    got = search_works(query, cache_path=cache_path, transport=transport)
                    budget.take()
                works = [
                    {**w, "abstract": (w["abstract"][:MAX_ABSTRACT_CHARS] if w.get("abstract") else None)}
                    for w in got["works"]
                ]
                result = _search_result(
                    "searched",
                    f"{len(works)} works",
                    budget,
                    query,
                    works,
                    url=got["url"],
                    retrieved_at=got["retrieved_at"],
                    api_version=got["api_version"],
                    from_cache=got["from_cache"],
                )
    except (FetchError, ValueError) as err:
        result = _search_result("failed", f"{type(err).__name__}: {err}", budget, query)
    except Exception as err:  # noqa: BLE001 - the boundary: anything unexpected becomes data, never a crash
        result = _search_result("failed", f"{type(err).__name__}: {err}", budget, query)
    entry = {
        "ts": clock(),
        "event": "search",
        "query": result["query"],
        "outcome": result["outcome"],
        "detail": result["detail"],
        "works": len(result["works"]),
        "budget_left": result["budget_left"],
        "from_cache": result.get("from_cache"),
        "run_id": run_id,
        "tool_version": TOOL_VERSION,
    }
    try:
        _append_log(log_path, entry)
    except OSError as err:
        return _search_result(
            "failed", f"the audit log cannot be written, so this call is not on record ({err})", budget, query
        )
    return result


READ_OUTCOMES: Final = ("read", "not_in_store", "budget_spent", "bad_input", "failed")
MAX_READ_CHARS: Final = 6_000
READ_TOOL_DESCRIPTION: Final = (
    f"Read the stored full text of a paper, {MAX_READ_CHARS} characters at a time. Arguments: `identifier` (a DOI, "
    "PMID or PMCID, as for `fetch_paper`) and `offset` (characters from the start, default 0). The paper must "
    "already be in the store: call `fetch_paper` first. `outcome` is 'read' (`text` holds this stretch, "
    "`next_offset` is where to continue or null at the end, `total_chars` the whole length), 'not_in_store' (fetch "
    "it first, or it is paywalled and cannot be read), 'budget_spent' (you have read as many different papers as "
    "this stage allows; further pages of a paper you already opened stay free), 'bad_input' or 'failed'. Reading "
    "a paper is not evidence that it says what you need: quote it only through `check_quote`."
)


@dataclass
class ReadBudget:
    """How many different papers this stage may still open. Spent by the first page of a paper; its later pages
    and a repeat are free, so reading a paper to the end costs what reading its first page costs."""

    left: int
    papers: set[str] = field(default_factory=set)

    def take(self, paper: str) -> None:
        self.papers.add(paper)
        self.left = max(self.left - 1, 0)


def _read_result(outcome: str, detail: str, budget: ReadBudget, identifier: Any, **extra: Any) -> dict[str, Any]:
    return {
        "outcome": outcome,
        "detail": detail,
        "identifier": identifier if isinstance(identifier, str) else repr(identifier),
        "budget_left": budget.left,
        "tool_version": TOOL_VERSION,
        **extra,
    }


def read_paper(
    store: LiteratureStore,
    log_path: Path,
    identifier: Any,
    offset: Any,
    budget: ReadBudget,
    *,
    run_id: str | None = None,
    clock: Callable[[], str] = _utc_now,
) -> dict[str, Any]:
    """The tool body. Always returns a result dict; every call leaves one audit line naming the paper and the range.

    Read-only on the store. The text is served as stored, with the sha256 of the file it came from, so a reader
    can check any stretch against the store. Nothing here decides whether a paper supports anything.
    """
    try:
        if not isinstance(identifier, str) or not identifier.strip():
            result = _read_result("bad_input", "`identifier` must be a non-empty string", budget, identifier)
        elif not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            result = _read_result("bad_input", "`offset` must be a non-negative integer", budget, identifier)
        elif (canonical := canonical_id(identifier)) is None:
            result = _read_result("bad_input", "not a DOI, PMID or PMCID", budget, identifier)
        elif (record := store.lookup(canonical)) is None or record.status == "not_available" or not record.text_path:
            result = _read_result(
                "not_in_store",
                "the paper is not in the store with a text: call `fetch_paper` "
                "first; if that says `paywalled`, it cannot be read",
                budget,
                identifier,
                paper=canonical,
            )
        elif canonical not in budget.papers and budget.left <= 0:
            result = _read_result(
                "budget_spent",
                f"this stage has already opened {len(budget.papers)} papers",
                budget,
                identifier,
                paper=record.canonical_id,
            )
        else:
            text = store.resolve(record.text_path).read_text(encoding="utf-8")
            if canonical not in budget.papers:
                budget.take(canonical)
            end = min(offset + MAX_READ_CHARS, len(text))
            result = _read_result(
                "read",
                f"characters {offset}-{end} of {len(text)}",
                budget,
                identifier,
                paper=record.canonical_id,
                text=text[offset:end],
                offset=offset,
                next_offset=end if end < len(text) else None,
                total_chars=len(text),
                text_status=record.status,
                text_sha256=record.sha256,
            )
    except Exception as err:  # noqa: BLE001 - the boundary: anything unexpected becomes data, never a crash
        result = _read_result("failed", f"{type(err).__name__}: {err}", budget, identifier)
    entry = {
        "ts": clock(),
        "event": "read",
        "identifier": result["identifier"],
        "paper": result.get("paper"),
        "outcome": result["outcome"],
        "offset": offset if isinstance(offset, int) else repr(offset),
        "chars": len(result.get("text") or ""),
        "budget_left": result["budget_left"],
        "run_id": run_id,
        "tool_version": TOOL_VERSION,
    }
    try:
        _append_log(log_path, entry)
    except OSError as err:
        return _read_result(
            "failed",
            f"the audit log cannot be written, so this call is not on record ({err})",
            budget,
            identifier,
            paper=result.get("paper"),
        )
    return result


def _store_snapshot(store: LiteratureStore) -> list[dict[str, Any]]:
    """Which papers the store held when the server started (ids, aliases, status, text hash; never the texts):
    lets an evaluator tell a paper that was checkable from one that was not, from the run directory alone."""
    return [
        {"canonical_id": r.canonical_id, "aliases": list(r.aliases), "status": r.status, "sha256": r.sha256}
        for r in store.records()
    ]


def build_server(
    store: LiteratureStore,
    log_path: Path,
    *,
    fetch_budget: int = 0,
    search_budget: int = 0,
    read_budget: int = 0,
    run_id: str | None = None,
    search_cache: Path = SEARCH_CACHE,
):
    """The MCP server object (imports the SDK here, and only here).

    `fetch_paper` is registered **only** when `fetch_budget > 0`, and that registration is the grant: a server
    present in a stage's MCP config is callable whatever `--allowedTools` says (`pipeline/launcher.py`), and
    this one server is registered for every phase. A phase with no budget therefore does not see the tool at
    all — which is what keeps it away from `verify`, whose verdict must rest on the quote it is handed.
    """
    from mcp.server.mcpserver import MCPServer

    server = MCPServer("literature")

    @server.tool(name="check_quote", description=TOOL_DESCRIPTION)
    def check_quote_tool(paper: str, quote: str) -> dict[str, Any]:
        return check_quote(store, log_path, paper, quote)

    if fetch_budget > 0:
        budget = FetchBudget(left=fetch_budget)

        @server.tool(name="fetch_paper", description=FETCH_TOOL_DESCRIPTION)
        def fetch_paper_tool(identifier: str) -> dict[str, Any]:
            return fetch_paper(store, log_path, identifier, budget, run_id=run_id)

    if search_budget > 0:
        searches = SearchBudget(left=search_budget)

        @server.tool(name="search_works", description=SEARCH_TOOL_DESCRIPTION)
        def search_works_tool(query: str) -> dict[str, Any]:
            return search_literature(log_path, query, searches, run_id=run_id, cache_path=search_cache)

    if read_budget > 0:
        reads = ReadBudget(left=read_budget)

        @server.tool(name="read_paper", description=READ_TOOL_DESCRIPTION)
        def read_paper_tool(identifier: str, offset: int = 0) -> dict[str, Any]:
            return read_paper(store, log_path, identifier, offset, reads, run_id=run_id)

    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--store", type=Path, required=True, help="literature store directory (must exist)")
    parser.add_argument("--log", type=Path, required=True, help="audit log (JSONL, appended)")
    parser.add_argument(
        "--max-fetch",
        type=int,
        default=0,
        help="papers this stage's agents may bring into the store; 0 (the default) does not "
        "register `fetch_paper` at all",
    )
    parser.add_argument(
        "--max-search",
        type=int,
        default=0,
        help="OpenAlex searches this stage's agents may make; 0 (the default) does not register "
        "`search_works` at all",
    )
    parser.add_argument(
        "--max-read",
        type=int,
        default=0,
        help="different papers this stage's agents may open with `read_paper`; 0 (the default) "
        "does not register it at all",
    )
    parser.add_argument(
        "--search-cache",
        type=Path,
        default=SEARCH_CACHE,
        help="append-only cache of search answers (local data, never committed)",
    )
    parser.add_argument("--run-id", default=None, help="recorded as the `run_id` of any paper fetched here")
    parser.add_argument(
        "--check",
        action="store_true",
        help="only verify that the server could start (store readable, SDK importable), then exit; writes nothing",
    )
    args = parser.parse_args(argv)
    try:
        if not args.store.is_dir():
            raise ValueError(f"store directory does not exist: {args.store}")
        if min(args.max_fetch, args.max_search, args.max_read) < 0:
            raise ValueError("--max-fetch, --max-search and --max-read must be >= 0")
        store = LiteratureStore(args.store)
        list(store.records())  # an unreadable index stops the server here
        server = build_server(
            store,
            args.log,
            fetch_budget=args.max_fetch,  # imports the SDK: fails here if
            search_budget=args.max_search,
            read_budget=args.max_read,
            run_id=args.run_id,  # it is missing
            search_cache=args.search_cache,
        )
        if args.check:
            print("literature MCP server: ok", file=sys.stderr)
            return 0
        args.log.parent.mkdir(parents=True, exist_ok=True)
        # The snapshot is of the *start*: with `--max-fetch` above zero the store grows during the stage, and
        # what arrived after this line is in the `event: "fetch"` rows.
        _append_log(
            args.log,
            {
                "ts": _utc_now(),
                "event": "server_start",
                "store": str(args.store),
                "tool_version": TOOL_VERSION,
                "fetch_budget": args.max_fetch,
                "search_budget": args.max_search,
                "read_budget": args.max_read,
                "run_id": args.run_id,
                "store_snapshot": _store_snapshot(store),
            },
        )
    except (OSError, ValueError, ImportError) as err:
        print(f"literature MCP server: cannot start: {err}", file=sys.stderr)
        return 2
    server.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
