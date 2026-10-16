"""Tests for tools.data.netmhciipan (no network: the transport is a fake)."""

from __future__ import annotations

import json

import pytest

from tools.data import netmhciipan as nm

HEADER = "allele\tseq_num\tstart\tend\tlength\tcore_peptide\tpeptide\tscore\trank"


def _table(pep: str, alleles: list[str]) -> str:
    rows = [f"{a}\t1\t1\t15\t15\tCORECORE1\t{pep}\t0.1234\t{10 + i}.0" for i, a in enumerate(alleles)]
    return "\n".join([HEADER, *rows]) + "\n"


class FakeTransport:
    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, params):
        self.calls.append(dict(params))
        return _table(params["sequence_text"], params["allele"].split(","))


PEP = "VRHIWAPKTEQVLLS"
A1, A2 = "HLA-DRB1*14:01", "HLA-DRB1*03:01"


def test_second_call_is_served_from_cache(tmp_path):
    fake, cache = FakeTransport(), tmp_path / "c.jsonl"
    first = nm.predict(PEP, f"{A1},{A2}", cache_path=cache, transport=fake)
    again = nm.predict(PEP, [A1, A2], cache_path=cache, transport=fake)
    assert first == again and len(fake.calls) == 1
    assert [p.allele for p in first] == [A1, A2] and first[1].rank == 11.0


def test_only_missing_alleles_are_requested(tmp_path):
    fake, cache = FakeTransport(), tmp_path / "c.jsonl"
    nm.predict(PEP, A1, cache_path=cache, transport=fake)
    nm.predict(PEP, [A1, A2], cache_path=cache, transport=fake)
    assert fake.calls[1]["allele"] == A2


def test_offline_mode_raises_on_miss_and_never_calls_transport(tmp_path):
    fake = FakeTransport()
    with pytest.raises(nm.CacheMiss):
        nm.predict(PEP, A1, cache_path=tmp_path / "c.jsonl", transport=fake, allow_network=False)
    assert fake.calls == []


def test_refresh_appends_and_keeps_history(tmp_path):
    fake, cache = FakeTransport(), tmp_path / "c.jsonl"
    nm.predict(PEP, A1, cache_path=cache, transport=fake)
    nm.predict(PEP, A1, cache_path=cache, transport=fake, refresh=True)
    assert len(cache.read_text().splitlines()) == 2 and len(fake.calls) == 2


def test_cache_record_keeps_request_and_time(tmp_path):
    cache = tmp_path / "c.jsonl"
    nm.predict(PEP, A1, cache_path=cache, transport=FakeTransport())
    rec = json.loads(cache.read_text().splitlines()[0])
    assert rec["request"]["method"] == "netmhciipan_el" and rec["retrieved_at"] and rec["url"] == nm.API_URL


def test_html_error_page_fails_loudly_and_is_not_cached(tmp_path):
    cache = tmp_path / "c.jsonl"
    with pytest.raises(nm.MalformedResponse):
        nm.predict(PEP, A1, cache_path=cache, transport=lambda p: "<html>308</html>")
    assert not cache.exists()


def test_missing_allele_row_is_an_error(tmp_path):
    with pytest.raises(nm.MalformedResponse):
        nm.predict(PEP, f"{A1},{A2}", cache_path=tmp_path / "c.jsonl", transport=lambda p: _table(PEP, [A1]))


def test_wrong_peptide_length_rejected(tmp_path):
    with pytest.raises(ValueError):
        nm.predict("SHORT", A1, cache_path=tmp_path / "c.jsonl", transport=FakeTransport())
