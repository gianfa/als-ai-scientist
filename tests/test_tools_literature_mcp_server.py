"""Tests for the literature MCP server: the tool body directly, then one real stdio session."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tools.literature import mcp_server as srv
from tools.literature.store import LiteratureStore

PARA = (
    "Autoimmune responses are thought to have a key role in ALS pathology, and it is hypothesized that T cells "
    "contribute to the rapid loss of neurons. These responses are mediated by CD4+ T cells that release IL-5 "
    "(P = 0.001) in ALS. "
) * 12
PACKAGE_ROOT = Path(__file__).resolve().parent.parent
QUOTE = "Autoimmune responses are thought to have a key role in ALS pathology"


@pytest.fixture()
def env(tmp_path: Path) -> tuple[LiteratureStore, Path]:
    store = LiteratureStore(tmp_path / "lit", clock=lambda: "2026-09-30T12:00:00Z")
    store.add(
        identifiers=["doi:10.1000/full"],
        title="Full paper",
        year=2025,
        text=PARA,
        source_url=None,
        licence="cc by",
        extractor="test",
        run_id="r",
        alias="smith-2025-full",
    )
    store.add(
        identifiers=["doi:10.1000/abs"],
        title="Abstract only",
        year=2025,
        text="Short abstract about ALS pathology and more words.",
        source_url=None,
        licence=None,
        extractor="test",
        run_id="r",
        alias="doe-2025-abs",
    )
    store.add_unavailable(
        identifiers=["doi:10.1000/paywall"], title="Paywalled", reason="no open-access full text", run_id="r"
    )
    return store, tmp_path / "run" / "calls.jsonl"


def _call(env, paper, quote, **kw):
    store, log = env
    log.parent.mkdir(parents=True, exist_ok=True)
    return srv.check_quote(store, log, paper, quote, clock=lambda: "2026-09-30T12:00:00Z", **kw)


def _log(env) -> list[dict]:
    return [json.loads(line) for line in env[1].read_text(encoding="utf-8").splitlines()]


def test_exact_quote_is_checked_with_a_receipt(env) -> None:
    r = _call(env, "smith-2025-full", QUOTE)
    assert r["status"] == "checked" and r["verdict"] == "exact" and r["receipt_id"] and r["cause"] is None
    assert r["paper"] == "doi:10.1000/full" and r["text_sha256"]
    assert _call(env, "doi:10.1000/full", QUOTE)["receipt_id"] == r["receipt_id"]  # deterministic


def test_near_returns_the_passage_and_absent_does_not(env) -> None:
    near = _call(env, "smith-2025-full", "Autoimmune responses are thought ... contribute to the rapid loss of neurons")
    assert near["verdict"] == "near" and "rapid loss of neurons" in near["passage"]
    absent = _call(
        env, "smith-2025-full", "Treatment with the drug did not alter the course of the disease in any group"
    )
    assert absent["status"] == "checked" and absent["verdict"] == "absent" and absent["passage"] is None


def test_assessment_tells_a_dropped_quote_from_an_invented_one(env) -> None:
    store, _ = env
    store.add(
        identifiers=["doi:10.1000/jats"],
        title="Jats paper",
        year=2025,
        text=PARA,
        source_url=None,
        licence=None,
        extractor=srv.JATS_EXTRACTOR,
        run_id="r",
        alias="roe-2025-jats",
        extras="Table 1 Serum IL-5 concentration was 12.4 pg per millilitre in patients carrying the repeat expansion",
    )
    in_table = _call(
        env,
        "roe-2025-jats",
        "Serum IL-5 concentration was 12.4 pg per millilitre in patients carrying the repeat expansion",
    )
    assert in_table["verdict"] == "absent" and in_table["assessment"] == "in_paper_outside_main_text"
    assert in_table["located_in"] == "extras" and in_table["text_completeness"] == "full_jats"
    invented = _call(
        env, "roe-2025-jats", "Treatment with the drug did not alter the course of the disease in any group"
    )
    assert invented["assessment"] == "not_in_stored_text"
    pdf_like = _call(
        env, "smith-2025-full", "Treatment with the drug did not alter the course of the disease in any group"
    )
    assert pdf_like["assessment"] == "not_found_text_may_be_incomplete" and pdf_like["text_completeness"] == "partial"
    assert _log(env)[-1]["assessment"] == "not_found_text_may_be_incomplete"


@pytest.mark.parametrize(
    "paper, quote, cause",
    [
        ("doi:10.1000/none", QUOTE, "paper_not_in_store"),
        ("doi:10.1000/paywall", QUOTE, "text_not_available"),
        ("", QUOTE, "bad_input"),
        ("smith-2025-full", "   ", "bad_input"),
        ("smith-2025-full", "x " * 6000, "bad_input"),
        (None, QUOTE, "bad_input"),
    ],
)
def test_unverifiable_causes_are_named(env, paper, quote, cause) -> None:
    r = _call(env, paper, quote)
    assert r["status"] == "unverifiable" and r["cause"] == cause and r["verdict"] is None and r["receipt_id"] is None
    assert r["detail"]


def test_abstract_only_paper_cannot_prove_absence(env) -> None:
    assert _call(env, "doe-2025-abs", "Short abstract about ALS pathology and more words")["verdict"] == "exact"
    r = _call(env, "doe-2025-abs", "Autoimmune responses are thought to have a key role in this disease")
    assert r["status"] == "unverifiable" and r["cause"] == "text_incomplete"


def test_modified_text_is_reported_as_hash_mismatch(env) -> None:
    store, _ = env
    store.resolve(store.lookup("smith-2025-full").text_path).write_text(PARA + "tampered", encoding="utf-8")
    r = _call(env, "smith-2025-full", QUOTE)
    assert r["status"] == "unverifiable" and r["cause"] == "text_hash_mismatch"


def test_unexpected_error_becomes_service_error_not_an_exception(env, monkeypatch) -> None:
    def boom(*_a, **_k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(srv, "assess_quote", boom)
    r = _call(env, "smith-2025-full", QUOTE)
    assert r["status"] == "unverifiable" and r["cause"] == "service_error" and "disk on fire" in r["detail"]


def test_every_call_leaves_one_audit_line_including_failures(env) -> None:
    _call(env, "smith-2025-full", QUOTE)
    _call(env, "doi:10.1000/none", QUOTE)
    lines = _log(env)
    assert [l["status"] for l in lines] == ["checked", "unverifiable"]
    assert lines[0]["quote"] == QUOTE and lines[0]["receipt_id"] and lines[1]["cause"] == "paper_not_in_store"


def test_fail_closed_when_the_audit_log_cannot_be_written(env) -> None:
    store, log = env
    log.mkdir(parents=True)  # a directory where the file should be
    r = srv.check_quote(store, log, "smith-2025-full", QUOTE)
    assert r["status"] == "unverifiable" and r["cause"] == "service_error" and r["verdict"] is None


def test_server_refuses_to_start_on_a_missing_store(tmp_path: Path, capsys) -> None:
    assert srv.main(["--store", str(tmp_path / "missing"), "--log", str(tmp_path / "calls.jsonl")]) == 2
    assert "cannot start" in capsys.readouterr().err


def test_real_stdio_session(env) -> None:
    pytest.importorskip("mcp")
    store, log = env
    log.parent.mkdir(parents=True, exist_ok=True)
    messages = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "check_quote", "arguments": {"paper": "smith-2025-full", "quote": QUOTE}},
        },
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "check_quote", "arguments": {"paper": "nope", "quote": QUOTE}},
        },
    ]
    proc = subprocess.Popen(
        [sys.executable, "-m", "tools.literature.mcp_server", "--store", str(store.root), "--log", str(log)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=PACKAGE_ROOT,
    )
    replies: dict[int, dict] = {}
    try:
        for message in messages:  # stdin stays open until every answer has arrived
            proc.stdin.write(json.dumps(message) + "\n")
            proc.stdin.flush()
        while not {2, 3, 4} <= replies.keys():  # replies may arrive out of order
            line = proc.stdout.readline()
            assert line, f"server closed early; stderr: {proc.stderr.read()}"
            reply = json.loads(line)
            if "id" in reply:
                replies[reply["id"]] = reply
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)
    assert [t["name"] for t in replies[2]["result"]["tools"]] == ["check_quote"]  # no budget: no fetch tool
    ok = replies[3]["result"]["structuredContent"]
    assert ok["status"] == "checked" and ok["verdict"] == "exact"
    bad = replies[4]["result"]["structuredContent"]
    assert bad["status"] == "unverifiable" and bad["cause"] == "paper_not_in_store"
    events = _log((store, log))
    assert events[0]["event"] == "server_start" and len(events) == 3


def test_check_mode_verifies_startup_and_writes_nothing(env, capsys) -> None:
    pytest.importorskip("mcp")
    store, log = env
    assert srv.main(["--store", str(store.root), "--log", str(log), "--check"]) == 0
    assert "ok" in capsys.readouterr().err
    assert not log.exists() and not log.parent.exists()  # nothing created, not even the log folder
    assert srv.main(["--store", str(store.root / "nope"), "--log", str(log), "--check"]) == 2


# --- `fetch_paper`: the agent locates, the code decides --------------------------------

NEW_SENTENCE = "C9orf72-specific CD4 T cells secrete IFN-gamma in patients with amyotrophic lateral sclerosis"
NEW_PARA = (NEW_SENTENCE + ". ") * 40


def _fetch(env, identifier, budget, **kw):
    store, log = env
    log.parent.mkdir(parents=True, exist_ok=True)
    return srv.fetch_paper(store, log, identifier, budget, clock=lambda: "2026-09-30T12:00:00Z", **kw)


def _refusing_transport(url):
    raise AssertionError(f"no network call should have been made ({url})")


def _europe_pmc(pmcid="PMC999", doi="10.1000/new", text=NEW_PARA):
    """Europe PMC's two endpoints, as `tests/test_tools_literature_fetch.py` fakes them: a search then a JATS."""
    calls: list[str] = []

    def transport(url):
        calls.append(url)
        if "search" in url:
            return json.dumps(
                {
                    "resultList": {
                        "result": [
                            {
                                "id": "1",
                                "pmcid": pmcid,
                                "doi": doi,
                                "pmid": "1",
                                "title": "A new paper",
                                "pubYear": "2025",
                                "isOpenAccess": "Y",
                                "authorString": "Rossi A",
                                "license": "cc by",
                            }
                        ]
                    }
                }
            )
        return f"<article><body><p>{text}</p></body></article>"

    return transport, calls


@pytest.mark.parametrize(
    "identifier",
    [
        "a paper about ALS",
        "https://www.news.uzh.ch/en/articles/2025/als.html",
        "Treatment of multiple sclerosis with peptide-coupled red blood cells",
        "",
        12345,
    ],
)
def test_only_a_doi_pmid_or_pmcid_can_be_fetched(env, identifier) -> None:
    """The doctrine: the agent locates, the code decides. A title, a URL or a sentence names no paper the
    resolver can ask for, so the call is refused before any network call and costs no budget."""
    budget = srv.FetchBudget(left=3)
    result = _fetch(env, identifier, budget, transport=_refusing_transport, pdf_transport=None)
    assert result["outcome"] == "bad_input"
    assert budget.left == 3 and budget.spent == 0
    assert _log(env)[-1]["event"] == "fetch"


def test_a_paper_already_in_the_store_costs_no_budget_and_no_network(env) -> None:
    budget = srv.FetchBudget(left=1)
    result = _fetch(env, "doi:10.1000/full", budget, transport=_refusing_transport, pdf_transport=None)
    assert result["outcome"] == "already_in_store" and result["paper"] == "doi:10.1000/full"
    assert budget.left == 1 and budget.spent == 0


def test_a_fetched_paper_is_checkable_by_the_very_next_call(env) -> None:
    """The whole point: an agent refused `paper_not_in_store` on a resolvable identifier can now close it
    itself. `LiteratureStore.lookup` re-reads the index on every call, so no restart is needed."""
    store, _ = env
    refused = _call(env, "doi:10.1000/new", NEW_SENTENCE)
    assert refused["status"] == "unverifiable" and refused["cause"] == "paper_not_in_store"

    transport, calls = _europe_pmc()
    budget = srv.FetchBudget(left=2)
    result = _fetch(env, "doi:10.1000/new", budget, transport=transport, pdf_transport=None, run_id="RUN-1")
    assert result["outcome"] == "fetched", result
    assert budget.left == 1 and budget.spent == 1 and calls

    checked = _call(env, "doi:10.1000/new", NEW_SENTENCE)
    assert checked["status"] == "checked" and checked["verdict"] == "exact"
    assert store.lookup("doi:10.1000/new").run_id == "RUN-1"


def test_a_spent_budget_refuses_without_a_network_call(env) -> None:
    budget = srv.FetchBudget(left=0, spent=4)
    result = _fetch(env, "doi:10.1000/new", budget, transport=_refusing_transport, pdf_transport=None)
    assert result["outcome"] == "budget_spent" and "4" in result["detail"]
    assert result["budget_left"] == 0


def test_a_paywalled_paper_is_recorded_once_and_then_answered_without_the_network(env) -> None:
    """`paywalled` writes the same `not_available` record the host's own fetch writes, so the second ask is
    free. It also costs no budget: an agent is never punished for asking."""

    def transport(url):
        if "search" in url:
            return json.dumps(
                {
                    "resultList": {
                        "result": [
                            {
                                "id": "1",
                                "doi": "10.1000/closed",
                                "pmid": "2",
                                "title": "Closed",
                                "pubYear": "2024",
                                "isOpenAccess": "N",
                            }
                        ]
                    }
                }
            )
        raise AssertionError("no full text should be requested")

    budget = srv.FetchBudget(left=2)
    first = _fetch(env, "doi:10.1000/closed", budget, transport=transport, pdf_transport=None)
    assert first["outcome"] == "paywalled" and budget.left == 2
    second = _fetch(env, "doi:10.1000/closed", budget, transport=_refusing_transport, pdf_transport=None)
    assert second["outcome"] == "known_unavailable"


def test_every_call_leaves_one_audit_line_that_the_quote_legs_ignore(env) -> None:
    """One line per call, served or not. The row carries no `quote` key and the tool is not `check_quote`, so
    `evaluation/quote_outcomes.py`'s two agent-leg filters exclude it and its row-count cross-check stays sound."""
    budget = srv.FetchBudget(left=1)
    _fetch(env, "not a paper", budget, transport=_refusing_transport, pdf_transport=None)
    _fetch(env, "doi:10.1000/full", budget, transport=_refusing_transport, pdf_transport=None)
    rows = [r for r in _log(env) if r.get("event") == "fetch"]
    assert len(rows) == 2
    assert [r["outcome"] for r in rows] == ["bad_input", "already_in_store"]
    assert all("quote" not in r for r in rows)


def test_an_unwritable_audit_log_refuses_the_call_but_states_what_happened(env, tmp_path) -> None:
    """Fail closed, as `check_quote` does — but the side effect may already have happened, so it is named."""
    store, _ = env
    transport, _calls = _europe_pmc()
    budget = srv.FetchBudget(left=1)
    result = srv.fetch_paper(
        store,
        tmp_path / "no-such-dir" / "calls.jsonl",
        "doi:10.1000/new",
        budget,
        transport=transport,
        pdf_transport=None,
    )
    assert result["outcome"] == "failed"
    assert "cannot be written" in result["detail"] and "fetched" in result["detail"]


def test_the_outcome_vocabulary_is_the_fetch_modules_own_plus_three(env) -> None:
    """One vocabulary for one fact: the words an agent reads here are the words the host's fetch records."""
    from tools.literature.fetch import OUTCOMES

    assert set(OUTCOMES) < set(srv.FETCH_OUTCOMES)
    assert set(srv.FETCH_OUTCOMES) - set(OUTCOMES) == {"bad_input", "already_in_store", "budget_spent"}


@pytest.mark.parametrize(
    "flags, expected",
    [
        ([], ["check_quote"]),
        (["--max-fetch", "0"], ["check_quote"]),
        (["--max-fetch", "2"], ["check_quote", "fetch_paper"]),
    ],
)
def test_the_tool_exists_only_where_a_budget_grants_it(env, flags, expected) -> None:
    """The grant is `--max-fetch`: with no budget the tool is not registered at all, which is what keeps it out
    of `verify`. `--allowedTools` only pre-approves (`pipeline/launcher.py`). Asked over the protocol, because
    what a phase can call is what the server advertises."""
    pytest.importorskip("mcp")
    store, log = env
    log.parent.mkdir(parents=True, exist_ok=True)
    messages = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    proc = subprocess.Popen(
        [sys.executable, "-m", "tools.literature.mcp_server", "--store", str(store.root), "--log", str(log), *flags],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=PACKAGE_ROOT,
    )
    replies: dict[int, dict] = {}
    try:
        for message in messages:
            proc.stdin.write(json.dumps(message) + "\n")
            proc.stdin.flush()
        while 2 not in replies:
            line = proc.stdout.readline()
            assert line, f"server closed early; stderr: {proc.stderr.read()}"
            reply = json.loads(line)
            if "id" in reply:
                replies[reply["id"]] = reply
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)
    assert sorted(t["name"] for t in replies[2]["result"]["tools"]) == expected


def test_a_negative_budget_is_refused_at_startup(env, capsys) -> None:
    store, log = env
    assert srv.main(["--store", str(store.root), "--log", str(log), "--max-fetch", "-1"]) == 2
    assert "must be >= 0" in capsys.readouterr().err


def test_a_retried_fetch_reports_and_logs_how_many_times_it_was_refused(env) -> None:
    """The provenance rules require a run to record "errors/retries", and stderr
    would not do: the MCP server is a grandchild of the launcher, and every `*.launcher.log` in the archives
    is 0 bytes although the server prints a startup line. The audit row is the channel that works."""
    import urllib.error

    from tools.literature import fetch as lf

    refused = {"n": 0}
    good, _calls = _europe_pmc()

    def transport(url):
        if "search" in url and refused["n"] < 2:
            refused["n"] += 1
            raise urllib.error.HTTPError(url, 429, "slow down", hdrs=None, fp=None)  # type: ignore[arg-type]
        return good(url)

    budget = srv.FetchBudget(left=1)
    original = lf.Retrying

    def with_no_sleep(inner, **kw):
        """The real policy, without the waiting: `original` is bound, not looked up, or it would recurse."""
        return original(inner, sleep=lambda _s: None, **kw)

    lf.Retrying = with_no_sleep
    try:
        result = _fetch(env, "doi:10.1000/new", budget, transport=transport, pdf_transport=None, run_id="RUN-1")
    finally:
        lf.Retrying = original

    assert result["outcome"] == "fetched" and result["retries"] == 2
    row = [r for r in _log(env) if r.get("event") == "fetch"][-1]
    assert row["retries"] == 2 and row["outcome"] == "fetched"


# --- search_works ---------------------------------------------------------------------------------------------

_SEARCH_BODY = {
    "results": [
        {
            "id": "https://openalex.org/W1",
            "doi": "https://doi.org/10.1000/a",
            "title": "T",
            "publication_year": 2024,
            "cited_by_count": 1,
            "abstract_inverted_index": {"word": [0]},
        }
    ]
}


class _SearchFake:
    def __init__(self, body=_SEARCH_BODY):
        self.body, self.calls = body, 0

    def __call__(self, url: str) -> str:
        if "/works" in url:
            self.calls += 1
            return json.dumps(self.body)
        return json.dumps({"version": "9.9"})


def _search(tmp_path: Path, query, budget: int, fake=None, name: str = "calls.jsonl"):
    fake = fake or _SearchFake()
    b = srv.SearchBudget(left=budget)
    out = srv.search_literature(
        tmp_path / name, query, b, cache_path=tmp_path / "cache.jsonl", transport=fake, clock=lambda: "T"
    )
    return out, b, fake


def test_search_spends_the_budget_only_on_the_network_and_logs_every_call(tmp_path: Path):
    out, budget, fake = _search(tmp_path, "treg vaccine", 1)
    assert out["outcome"] == "searched" and out["works"][0]["abstract"] == "word" and budget.left == 0
    again = srv.search_literature(
        tmp_path / "calls.jsonl",
        "treg vaccine",
        budget,
        cache_path=tmp_path / "cache.jsonl",
        transport=fake,
        clock=lambda: "T",
    )
    assert again["outcome"] == "searched" and again["from_cache"] is True and fake.calls == 1
    lines = [json.loads(x) for x in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert [x["event"] for x in lines] == ["search", "search"] and lines[0]["query"] == "treg vaccine"


def test_search_refuses_a_new_query_once_the_budget_is_spent(tmp_path: Path):
    out, _, fake = _search(tmp_path, "new query", 0)
    assert out["outcome"] == "budget_spent" and out["works"] == [] and fake.calls == 0


@pytest.mark.parametrize("query", ["", "  ", None, 5])
def test_search_bad_input_is_data_not_a_crash(tmp_path: Path, query):
    out, budget, fake = _search(tmp_path, query, 3)
    assert out["outcome"] == "bad_input" and budget.left == 3 and fake.calls == 0


def test_search_service_failure_is_data_and_costs_nothing(tmp_path: Path):
    out, budget, _ = _search(tmp_path, "q", 2, _SearchFake({"unexpected": 1}))
    assert out["outcome"] == "failed" and budget.left == 2


def test_search_long_abstracts_are_shortened(tmp_path: Path):
    body = {"results": [{"id": "W", "title": "T", "abstract_inverted_index": {"w": list(range(900))}}]}
    out, _, _ = _search(tmp_path, "q", 1, _SearchFake(body))
    assert len(out["works"][0]["abstract"]) == srv.MAX_ABSTRACT_CHARS


def test_search_fails_closed_when_the_audit_line_cannot_be_written(tmp_path: Path):
    (tmp_path / "calls.jsonl").mkdir()
    out, _, _ = _search(tmp_path, "q", 1)
    assert out["outcome"] == "failed" and "not on record" in out["detail"]


def test_search_tool_is_registered_only_with_a_budget(tmp_path: Path):
    pytest.importorskip("mcp")
    import asyncio

    store = LiteratureStore(tmp_path / "store")

    def names(**kw):
        server = srv.build_server(store, tmp_path / "log.jsonl", **kw)
        return {t.name for t in asyncio.run(server.list_tools())}

    assert "search_works" not in names() and "search_works" in names(search_budget=2)


# --- read_paper -----------------------------------------------------------------------------------------------


def _reader(tmp_path: Path, budget: int = 1):
    store = LiteratureStore(tmp_path / "store")
    return store, srv.ReadBudget(left=budget), tmp_path / "log.jsonl"


def test_read_paper_refuses_what_is_not_in_the_store_and_tells_to_fetch(tmp_path):
    store, budget, log = _reader(tmp_path)
    out = srv.read_paper(store, log, "10.1000/missing", 0, budget, clock=lambda: "T")
    assert out["outcome"] == "not_in_store" and "fetch_paper" in out["detail"] and budget.left == 1
    assert json.loads(log.read_text())["event"] == "read"


@pytest.mark.parametrize(
    "identifier,offset",
    [("", 0), (None, 0), ("a title, not an id", 0), ("10.1000/a", -1), ("10.1000/a", "0"), ("10.1000/a", True)],
)
def test_read_paper_bad_input_is_data(tmp_path, identifier, offset):
    store, budget, log = _reader(tmp_path)
    assert srv.read_paper(store, log, identifier, offset, budget)["outcome"] == "bad_input"


def test_read_paper_budget_is_per_different_paper_and_pages_are_free(tmp_path, monkeypatch):
    store, budget, log = _reader(tmp_path, budget=1)
    from types import SimpleNamespace

    text = "x" * (srv.MAX_READ_CHARS + 100)
    records = {
        "doi:10.1000/a": SimpleNamespace(canonical_id="doi:10.1000/a", status="full_text", text_path="a", sha256="s"),
        "doi:10.1000/b": SimpleNamespace(canonical_id="doi:10.1000/b", status="full_text", text_path="b", sha256="s"),
    }
    monkeypatch.setattr(store, "lookup", lambda c, verify=True: records.get(c))
    monkeypatch.setattr(store, "resolve", lambda p: SimpleNamespace(read_text=lambda encoding=None: text))

    first = srv.read_paper(store, log, "10.1000/a", 0, budget)
    assert first["outcome"] == "read" and len(first["text"]) == srv.MAX_READ_CHARS
    assert first["next_offset"] == srv.MAX_READ_CHARS and first["total_chars"] == len(text) and budget.left == 0
    page2 = srv.read_paper(store, log, "10.1000/a", first["next_offset"], budget)
    assert page2["outcome"] == "read" and page2["next_offset"] is None and len(page2["text"]) == 100
    assert srv.read_paper(store, log, "10.1000/b", 0, budget)["outcome"] == "budget_spent"


def test_read_paper_is_registered_only_with_a_budget(tmp_path):
    pytest.importorskip("mcp")
    import asyncio

    store = LiteratureStore(tmp_path / "store")

    def names(**kw):
        server = srv.build_server(store, tmp_path / "log.jsonl", **kw)
        return {t.name for t in asyncio.run(server.list_tools())}

    assert "read_paper" not in names() and "read_paper" in names(read_budget=2)
