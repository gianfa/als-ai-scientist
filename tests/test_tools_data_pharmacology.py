"""Tests for tools.data.pharmacology (no network: the transport is a fake)."""

from __future__ import annotations

import json

import pytest

from tools.data import pharmacology as ph

HIT = {
    "drugbank.id": "DB00877",
    "drugbank.name": "Sirolimus",
    "chembl.molecule_chembl_id": "CHEMBL413",
    "chembl.pref_name": "SIROLIMUS",
    "chebi.id": "CHEBI:9168",
    "chebi.name": "sirolimus",
    "pubchem.cid": 5284616,
    "unii.unii": "W36ZG6FT64",
    "unii.display_name": "SIROLIMUS",
}


def _body(total: int, hits: list[dict]) -> str:
    return json.dumps({"took": 1, "total": total, "hits": hits})


class FakeTransport:
    def __init__(self, responses: dict[str, str] | None = None):
        self.calls: list[str] = []
        self.responses = responses or {}

    def __call__(self, url: str) -> str:
        self.calls.append(url)
        for name, body in self.responses.items():
            if urllib_quoted(name) in url:
                return body
        return self.responses.get("*", _body(0, []))


def urllib_quoted(name: str) -> str:
    import urllib.parse

    return urllib.parse.quote(name.strip())


def test_exact_name_match_is_found(tmp_path):
    cache = tmp_path / "c.jsonl"
    fake = FakeTransport({"sirolimus": _body(1, [HIT])})
    record = ph.lookup_drug("sirolimus", cache_path=cache, transport=fake)
    assert record.drugbank_id == "DB00877" and record.chembl_id == "CHEMBL413"
    assert record.chebi_id == "CHEBI:9168" and record.pubchem_cid == "5284616"
    assert record.unii == "W36ZG6FT64" and record.tradename == "Sirolimus"
    assert record.source_url.startswith(ph.API_URL)


def test_second_call_is_served_from_cache(tmp_path):
    cache = tmp_path / "c.jsonl"
    fake = FakeTransport({"sirolimus": _body(1, [HIT])})
    first = ph.lookup_drug("sirolimus", cache_path=cache, transport=fake)
    again = ph.lookup_drug("sirolimus", cache_path=cache, transport=fake)
    assert first == again and len(fake.calls) == 1


def test_offline_mode_raises_on_miss_and_never_calls_transport(tmp_path):
    fake = FakeTransport()
    with pytest.raises(ph.CacheMiss):
        ph.lookup_drug("sirolimus", cache_path=tmp_path / "c.jsonl", transport=fake, allow_network=False)
    assert fake.calls == []


def test_refresh_appends_and_keeps_history(tmp_path):
    cache = tmp_path / "c.jsonl"
    fake = FakeTransport({"sirolimus": _body(1, [HIT])})
    ph.lookup_drug("sirolimus", cache_path=cache, transport=fake)
    ph.lookup_drug("sirolimus", cache_path=cache, transport=fake, refresh=True)
    assert len(cache.read_text().splitlines()) == 2 and len(fake.calls) == 2


def test_cache_record_keeps_request_and_time(tmp_path):
    cache = tmp_path / "c.jsonl"
    ph.lookup_drug("sirolimus", cache_path=cache, transport=FakeTransport({"sirolimus": _body(1, [HIT])}))
    rec = json.loads(cache.read_text().splitlines()[0])
    assert rec["request"]["name"] == "sirolimus" and rec["retrieved_at"] and rec["url"].startswith(ph.API_URL)


def test_confirmed_miss_is_cached_and_not_requeried(tmp_path):
    cache = tmp_path / "c.jsonl"
    fake = FakeTransport({"*": _body(0, [])})
    first = ph.lookup_drug("zzznotarealdrugzzz", cache_path=cache, transport=fake)
    again = ph.lookup_drug("zzznotarealdrugzzz", cache_path=cache, transport=fake)
    assert first is None and again is None and len(fake.calls) == 1
    rec = json.loads(cache.read_text().splitlines()[0])
    assert rec["match"] == "none"


def test_ambiguous_match_is_none_but_distinct_from_a_miss_in_the_cache(tmp_path):
    other = dict(HIT, **{"drugbank.id": "DB99999", "drugbank.name": "Something Else"})
    hits = [
        dict(HIT, **{"drugbank.name": "rapamycin-like-one"}),
        dict(other, **{"drugbank.name": "rapamycin"}),
        dict(HIT, **{"drugbank.name": "rapamycin", "drugbank.id": "DB00001"}),
    ]
    cache = tmp_path / "c.jsonl"
    fake = FakeTransport({"rapamycin": _body(3, hits)})
    result = ph.lookup_drug("rapamycin", cache_path=cache, transport=fake)
    assert result is None
    rec = json.loads(cache.read_text().splitlines()[0])
    assert rec["match"] == "ambiguous"


def test_a_single_hit_with_no_exact_name_is_still_found(tmp_path):
    synonym_hit = dict(HIT, **{"drugbank.name": "Sirolimus"})  # "rapamycin" query, no exact-name field match
    cache = tmp_path / "c.jsonl"
    fake = FakeTransport({"rapamycin": _body(1, [synonym_hit])})
    record = ph.lookup_drug("rapamycin", cache_path=cache, transport=fake)
    assert record is not None and record.drugbank_id == "DB00877"


def test_a_synonym_with_several_unrelated_hits_and_no_exact_name_is_not_found(tmp_path):
    hits = [dict(HIT, **{"drugbank.name": "Sirolimus"}), {"chebi.id": "CHEBI:1", "chebi.name": "something else"}]
    cache = tmp_path / "c.jsonl"
    fake = FakeTransport({"rapamycin": _body(2, hits)})
    record = ph.lookup_drug("rapamycin", cache_path=cache, transport=fake)
    assert record is None


def test_html_error_page_fails_loudly_and_is_not_cached(tmp_path):
    cache = tmp_path / "c.jsonl"
    with pytest.raises(ph.MalformedResponse):
        ph.lookup_drug("sirolimus", cache_path=cache, transport=lambda url: "<html>308</html>")
    assert not cache.exists()


def test_response_missing_hits_key_is_malformed(tmp_path):
    cache = tmp_path / "c.jsonl"
    with pytest.raises(ph.MalformedResponse):
        ph.lookup_drug("sirolimus", cache_path=cache, transport=lambda url: json.dumps({"ok": True}))
    assert not cache.exists()


def test_empty_name_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        ph.lookup_drug("   ", cache_path=tmp_path / "c.jsonl", transport=FakeTransport())


def test_a_list_valued_identifier_is_kept_whole_not_cut_to_its_first(tmp_path):
    # Live, 2026-10-10: metformin's pubchem.cid is [152743144, 155906370, 4091]; the parent compound is not first.
    body = json.dumps(
        {
            "total": 1,
            "hits": [
                {"drugbank.name": "Metformin", "drugbank.id": "DB00331", "pubchem.cid": [152743144, 155906370, 4091]}
            ],
        }
    )
    record = ph.lookup_drug("metformin", cache_path=tmp_path / "c.jsonl", transport=lambda url: body)
    assert record.pubchem_cid == "152743144, 155906370, 4091"
