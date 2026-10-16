"""Deterministic provenance check of proposed claims.

A literature claim is `verified` only when its `quote` is found **exactly** in the stored text of its `source`
(`tools.literature`, the same check and audit log as the `check_quote` MCP tool). Anything else (no source, no
quote, paper not in the store, `near`, `absent`, unverifiable) stays `unverified`. The check says the passage is
in the paper; it does not say the claim is true, nor that the claim's wording is faithful to the passage (that is
for the independent `verify` agent, later).

`fetch_missing` brings the cited papers into the store first (Europe PMC open access, then OpenAlex
`publishedVersion` PDFs for what Europe PMC cannot serve; capped by `max_new`), so a claim citing a paper the store
did not hold yet can still be checked.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from tools.literature.fetch import BytesTransport, Transport, fetch_reading_list, http_bytes_transport, http_transport
from tools.literature.mcp_server import check_quote
from tools.literature.store import LiteratureStore

from .proposals import ProposedClaim, Provenance


class QuoteVerifier:
    def __init__(self, store: LiteratureStore, log_path: Path, check: Callable[..., dict] = check_quote) -> None:
        self.store, self.log_path, self._check = store, log_path, check
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, claim: ProposedClaim) -> Provenance:
        if not claim.source or not claim.quote:
            return Provenance("unverified", "none")
        result = self._check(self.store, self.log_path, claim.source, claim.quote)
        if result.get("status") == "checked" and result.get("verdict") == "exact":
            return Provenance("verified", "quote", (f"receipt:{result['receipt_id']}",))
        return Provenance("unverified", "none")


def fetch_outcome(value: Any) -> tuple[str, str]:
    """One `fetched` entry as (outcome, reason), from either shape the archives carry.

    `retries`, added later, is read by key where it is wanted and ignored here: this function answers
    what happened, and a retry is how long it took to find out.

    The value is `{"outcome", "reason"}`, because the outcome alone cannot be acted on:
    `paywalled` covers both "Europe PMC has no open-access text" and "and OpenAlex has no publishedVersion PDF
    either", and only the second says the paper is closed to both routes we have. Every archive written before
    that carries a bare outcome string and must keep reading, with its reason reported as unrecorded rather
    than invented.
    """
    if isinstance(value, dict):
        return str(value.get("outcome") or "unknown"), str(value.get("reason") or "")
    return str(value), ""


def fetch_missing(
    sources: Iterable[str],
    store: LiteratureStore,
    run_id: str,
    max_new: int,
    transport: Transport = http_transport,
    pdf_transport: BytesTransport | None = http_bytes_transport,
) -> dict[str, dict[str, str]]:
    """Fetch the cited papers the store lacks; returns identifier -> {outcome, reason}, for the run log.

    The **reason** is kept because the outcome alone cannot be acted on: `paywalled` covers both "Europe PMC
    has no open-access text" and "and OpenAlex has no publishedVersion PDF either", and only the second says
    the paper is closed to both routes we have. The store keeps the same sentence in the record's `note`; this
    puts it in the run's own archive, where the evaluation reads it.

    Archives written before this change carry identifier -> outcome as a bare string; readers take both shapes
    (`evaluation.quote_outcomes.fetch_outcome`).

    `pdf_transport=None` disables the OpenAlex fallback.
    """
    missing = [s for s in dict.fromkeys(sources) if store.lookup(s, verify=False) is None]
    if not missing or max_new == 0:
        return {s: {"outcome": "not_fetched", "reason": "the phase's fetch budget was already spent"} for s in missing}
    results = fetch_reading_list(missing, store, transport, max_new=max_new, run_id=run_id, pdf_transport=pdf_transport)
    # `retries` is the extra requests a refused 429 or 503 cost that paper. It is in the report because
    # The provenance rules require a run to record "errors/retries", and because a
    # phase whose fetches were throttled is a different phase from one whose papers were simply absent.
    return {r.identifier: {"outcome": r.outcome, "reason": r.detail, "retries": r.retries} for r in results}
