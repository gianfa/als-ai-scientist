"""Tests for scripts/data/paper-download.py. No network: the registry answers are injected.

The script's claim is narrow and worth testing exactly: it states what the registries say about a licence and
fetches only what one of them calls open. So the cases are the notice's content, the refusal when no open
licence is stated, and the order in which locations are tried.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DOI = "10.1038/s41586-025-09588-6"
CC = "https://creativecommons.org/licenses/by-nc-nd/4.0"

CROSSREF_OPEN = {
    "message": {
        "title": ["Autoimmune response to C9orf72 protein in amyotrophic lateral sclerosis"],
        "container-title": ["Nature"],
        "issued": {"date-parts": [[2025, 10, 1]]},
        "license": [{"URL": CC + "/", "content-version": "vor", "start": {"date-time": "2025-10-01T00:00:00Z"}}],
        "link": [
            {"content-type": "application/pdf", "URL": "https://publisher.example/a.pdf", "content-version": "vor"}
        ],
    }
}
CROSSREF_CLOSED = {
    "message": {"title": ["A paywalled paper"], "container-title": ["Journal"], "issued": {"date-parts": [[2024]]}}
}
EPMC_OPEN = {
    "resultList": {
        "result": [
            {
                "pmcid": "PMC12657233",
                "isOpenAccess": "Y",
                "license": "cc by-nc-nd",
                "fullTextUrlList": {
                    "fullTextUrl": [
                        {"availability": "Open access", "documentStyle": "pdf", "url": "https://epmc.example/p.pdf"},
                        {
                            "availability": "Subscription required",
                            "documentStyle": "doi",
                            "url": "https://doi.org/" + DOI,
                        },
                    ]
                },
            }
        ]
    }
}
EPMC_EMPTY: dict = {"resultList": {"result": []}}


def _load_module():
    path = REPO_ROOT / "scripts" / "data" / "paper-download.py"
    spec = importlib.util.spec_from_file_location("paper_download", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Registered before execution: a dataclass in a module with postponed annotations needs its own module in
    # `sys.modules` for `dataclasses` to resolve the field types (Python 3.10).
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _registries(module, crossref: dict, epmc: dict):
    def fake_get_json(url: str) -> dict:
        return crossref if url.startswith(module.CROSSREF) else epmc

    module._get_json = fake_get_json


def test_the_notice_states_the_licence_each_registry_states_and_the_clauses_of_its_code():
    module = _load_module()
    _registries(module, CROSSREF_OPEN, EPMC_OPEN)
    work = module.from_europe_pmc(DOI, module.from_crossref(DOI, module.Work(doi=DOI)))
    text = module.notice(work)
    assert CC in text and "stated by Crossref" in text and "stated by Europe PMC" in text
    assert "attribution" in text and "non-commercial" in text and "no derivatives" in text
    assert "PMC12657233" in text and "isOpenAccess=Y" in text
    assert "never committed" in text and "Not legal advice." in text


def test_a_work_with_no_stated_open_licence_is_refused_and_nothing_is_written(tmp_path, capsys):
    module = _load_module()
    _registries(module, CROSSREF_CLOSED, EPMC_EMPTY)
    dest = tmp_path / "paper.xml"
    code = module.main(["--doi", "10.9999/closed", "--dest", str(dest), "--yes"])
    assert code == 1
    assert not dest.exists()
    assert "no open licence" in capsys.readouterr().err


def test_an_open_access_location_is_tried_before_a_publisher_mining_link():
    module = _load_module()
    _registries(module, CROSSREF_OPEN, EPMC_OPEN)
    work = module.from_europe_pmc(DOI, module.from_crossref(DOI, module.Work(doi=DOI)))
    ordered = module._preferred(work.locations)
    assert "epmc.example" in ordered[0]["url"]


def test_duplicate_locations_are_listed_once():
    module = _load_module()
    twice = [{"url": "https://x.example/a.pdf", "kind": "publisher PDF (vor)", "stated_by": "Crossref"}] * 2
    assert len(module._preferred(twice)) == 1


def test_the_pdf_mode_prints_the_url_and_writes_nothing(tmp_path, capsys):
    module = _load_module()
    _registries(module, CROSSREF_OPEN, EPMC_OPEN)
    dest = tmp_path / "paper.pdf"
    code = module.main(["--doi", DOI, "--format", "pdf", "--dest", str(dest)])
    out = capsys.readouterr().out
    assert code == 0 and not dest.exists()
    lines = out.splitlines()
    # The link, not the licence notice: one URL, the open-access one first.
    assert lines[:4] == [
        "Please download the PDF:",
        "  https://epmc.example/p.pdf",
        "and save it as:",
        f"  {dest.resolve()}",
    ]  # where, as a full path
    assert lines[4] == "-------" and "download it yourself" in lines[5] and len(lines) == 6


def test_a_full_text_that_is_not_an_article_is_refused_rather_than_saved(tmp_path, monkeypatch):
    module = _load_module()
    _registries(module, CROSSREF_OPEN, EPMC_OPEN)
    work = module.from_europe_pmc(DOI, module.from_crossref(DOI, module.Work(doi=DOI)))

    class FakeResponse:
        headers = {"Content-Type": "text/html"}

        def read(self) -> bytes:
            return b"<html>not the article</html>"

        def __enter__(self):
            return self

        def __exit__(self, *exc) -> None:
            return None

    monkeypatch.setattr(module.urllib.request, "urlopen", lambda *a, **k: FakeResponse())
    dest = tmp_path / "paper.xml"
    with pytest.raises(RuntimeError, match="not a JATS article"):
        module.fetch_full_text(work, dest)
    assert not dest.exists()


def test_the_default_destinations_are_in_the_literature_inbox_not_in_a_developer_folder() -> None:
    """An end user has no local notes directory; the inbox is git-ignored and is where `ingest.py` reads papers added by hand."""
    module = _load_module()
    for kind in ("xml", "pdf"):
        dest = module.DEFAULT_DEST[kind]
        assert dest.parts[:2] == ("literature", "inbox"), dest
        assert dest.suffix == f".{kind}"
