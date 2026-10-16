"""Tests for tools.literature.search with a fake transport (no network)."""

from __future__ import annotations

import json
import urllib.parse
from pathlib import Path

import pytest

from tools.literature import search as ls
from tools.literature.fetch import FetchError

RESULTS = {
    "results": [
        {
            "id": "https://openalex.org/W1",
            "doi": "https://doi.org/10.1000/a",
            "title": "A first paper",
            "publication_year": 2024,
            "cited_by_count": 7,
            "abstract_inverted_index": {"Treg": [1], "Antigen-specific": [0], "cells": [2]},
        },
        {
            "id": "https://openalex.org/W2",
            "doi": None,
            "title": "No abstract",
            "publication_year": 2020,
            "cited_by_count": 0,
            "abstract_inverted_index": None,
        },
    ]
}


class Fake:
    def __init__(self, body=RESULTS):
        self.body, self.urls = body, []

    def __call__(self, url: str) -> str:
        self.urls.append(url)
        if url.rstrip("/").endswith("openalex.org"):
            return json.dumps({"version": "9.9"})
        return self.body if isinstance(self.body, str) else json.dumps(self.body)

    def searches(self) -> list[str]:
        return [u for u in self.urls if "/works" in u]


def test_abstract_is_rebuilt_in_position_order():
    assert ls.abstract_of({"cells": [2], "Antigen-specific": [0], "Treg": [1]}) == "Antigen-specific Treg cells"
    assert ls.abstract_of(None) is None and ls.abstract_of({}) is None


def test_search_returns_only_what_the_service_returned(tmp_path: Path):
    out = ls.search_works("Treg vaccine ALS", cache_path=tmp_path / "c.jsonl", transport=Fake())
    first, second = out["works"]
    assert first["doi"] == "10.1000/a" and first["abstract"] == "Antigen-specific Treg cells"
    assert second["doi"] is None and second["abstract"] is None
    assert out["api_version"] == "9.9" and out["from_cache"] is False
    assert "api.openalex.org/works" in out["url"]
    assert urllib.parse.parse_qs(urllib.parse.urlparse(out["url"]).query)["search"] == ["Treg vaccine ALS"]


def test_second_call_is_served_from_cache_without_the_network(tmp_path: Path):
    fake, cache = Fake(), tmp_path / "c.jsonl"
    ls.search_works("q one", cache_path=cache, transport=fake)
    again = ls.search_works("  Q   ONE ", cache_path=cache, transport=fake)
    assert again["from_cache"] is True and len(fake.searches()) == 1
    assert len(cache.read_text().splitlines()) == 1


def test_forbidden_network_on_a_miss_raises_and_writes_nothing(tmp_path: Path):
    with pytest.raises(ls.CacheMiss):
        ls.search_works("never asked", cache_path=tmp_path / "c.jsonl", transport=Fake(), allow_network=False)
    assert not (tmp_path / "c.jsonl").exists()


def test_refresh_appends_a_new_record_and_the_newest_wins(tmp_path: Path):
    cache = tmp_path / "c.jsonl"
    ls.search_works("q", cache_path=cache, transport=Fake())
    ls.search_works("q", cache_path=cache, transport=Fake({"results": []}), refresh=True)
    assert len(cache.read_text().splitlines()) == 2
    assert ls.search_works("q", cache_path=cache, transport=Fake(), allow_network=False)["works"] == []


@pytest.mark.parametrize("body", ["not json", {"unexpected": 1}, {"results": "x"}])
def test_a_malformed_answer_is_a_failure_not_an_empty_result(tmp_path: Path, body):
    with pytest.raises(FetchError):
        ls.search_works("q", cache_path=tmp_path / "c.jsonl", transport=Fake(body))
    assert not (tmp_path / "c.jsonl").exists()


@pytest.mark.parametrize("query,limit", [("", 5), ("   ", 5), ("q", 0), ("q", ls.MAX_LIMIT + 1)])
def test_invalid_arguments_are_refused(tmp_path: Path, query, limit):
    with pytest.raises(ValueError):
        ls.search_works(query, limit=limit, cache_path=tmp_path / "c.jsonl", transport=Fake())


def _refused(code: int, headers: dict | None = None):
    import email.message
    import urllib.error

    msg = email.message.Message()
    for k, v in (headers or {}).items():
        msg[k] = v
    return urllib.error.HTTPError("https://api.openalex.org/works", code, "refused", msg, None)


class Flaky(Fake):
    """Refuses the first `refusals` search requests, then answers."""

    def __init__(self, refusals: int, code: int = 429):
        super().__init__()
        self.refusals, self.code = refusals, code

    def __call__(self, url: str) -> str:
        if "/works" in url and self.refusals > 0:
            self.refusals -= 1
            self.urls.append(url)
            raise _refused(self.code)
        return super().__call__(url)


def test_a_refused_search_is_retried_with_the_shared_backoff(tmp_path: Path):
    from tools.literature.fetch import Retrying

    waits: list[float] = []
    flaky = Flaky(refusals=2)
    out = ls.search_works(
        "Treg vaccine ALS",
        cache_path=tmp_path / "c.jsonl",
        transport=Retrying(flaky, sleep=waits.append),
    )
    assert len(out["works"]) == 2 and len(flaky.searches()) == 3 and len(waits) == 2


def test_a_search_refused_beyond_the_retries_fails_loudly_and_caches_nothing(tmp_path: Path):
    from tools.literature.fetch import Retrying

    cache = tmp_path / "c.jsonl"
    with pytest.raises(FetchError, match="429"):
        ls.search_works("q", cache_path=cache, transport=Retrying(Flaky(refusals=9), sleep=lambda s: None))
    assert not cache.exists() or cache.read_text() == ""


def test_the_default_wrapping_retries_a_plain_transport(tmp_path: Path):
    # One refusal, so the jittered wait is at most BACKOFF_BASE_S (1 s) of real sleep.
    flaky = Flaky(refusals=1)
    out = ls.search_works("another query", cache_path=tmp_path / "c.jsonl", transport=flaky)
    assert out["from_cache"] is False and len(flaky.searches()) == 2


def test_a_forbidden_search_is_not_retried(tmp_path: Path):
    flaky = Flaky(refusals=5, code=403)
    with pytest.raises(FetchError, match="403"):
        ls.search_works("q403", cache_path=tmp_path / "c.jsonl", transport=flaky)
    assert len(flaky.searches()) == 1
