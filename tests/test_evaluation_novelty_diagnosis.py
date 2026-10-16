"""`evaluation.novelty_diagnosis`: a novelty stage that went wrong says so in its own archive."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evaluation import novelty_diagnosis as nd
from tools.literature.search import search_works


def _w(i: str, abstract: str | None = "a") -> dict:
    return {
        "id": f"https://openalex.org/{i}",
        "title": i,
        "abstract_inverted_index": {abstract: [0]} if abstract else None,
    }


class Fake:
    def __init__(self, works):
        self.works = works

    def __call__(self, url: str) -> str:
        return json.dumps({"results": self.works} if "/works" in url else {"version": "1"})


def _write(path: Path, rows) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def _stage(
    tmp_path: Path,
    *,
    queries=("treg als",),
    reported=("treg als",),
    cited=("https://openalex.org/W1",),
    entry_status="assessed",
    tool_calls=1,
    outcome="searched",
    works=None,
    provenance=...,
):
    results, cache = tmp_path / "results", tmp_path / "cache.jsonl"
    results.mkdir()
    for q in queries:
        search_works(q, cache_path=cache, transport=Fake(works if works is not None else [_w("W1")]))
    entry = {"hypothesis_id": "H-1", "status": entry_status}
    if entry_status == "assessed":
        entry["novelty"] = {
            "verdict": "anticipated",
            "queries": list(reported),
            "closest_works": [{"openalex_id": c} for c in cited],
        }
    else:
        entry["error"] = "works the search tool never returned: x"
    # `...` means the default block the host writes; `None` an archive from before the host wrote one.
    if provenance is ...:
        entry["provenance"] = {
            "kept": [{"openalex_id": c, "evidence_depth": "abstract"} for c in cited],
            "dropped": [],
            "rationale_only": [],
            "works_listed": len(cited),
            "works_dropped": 0,
            "works_read_in_full": 0,
        }
    elif provenance is not None:
        entry["provenance"] = provenance
    (results / nd.REPORT).write_text(
        json.dumps(
            {
                "status": "assessed" if entry_status == "assessed" else "unverified",
                "entries": [entry],
                "dropped_agents": [],
                "launch": {"session_cost_usd": 0.5, "wall_s": 40},
            }
        )
    )
    _write(
        results / nd.SERVER_LOG,
        [{"event": "fetch"}]
        + [{"event": "search", "query": q, "outcome": outcome, "from_cache": False} for q in queries],
    )
    _write(
        results / nd.TOOL_LOG,
        [
            {"label": "novelty@H-1", "tool": nd.SEARCH_TOOL, "status": "ok", "input_preview": json.dumps({"query": q})}
            for q in queries[:tool_calls]
        ],
    )
    return results, cache


def codes(data) -> set[str]:
    return {f["code"] for f in data["findings"]}


def test_a_clean_stage_has_no_problem(tmp_path):
    results, cache = _stage(tmp_path)
    data = nd.diagnose(results, cache)

    assert data["problems"] == 0 and data["figures"]["searches_logged"] == 1
    assert data["figures"]["cost_usd"] == 0.5 and data["entries"][0]["search_calls"] == 1
    assert nd.main([str(results), "--cache", str(cache)]) == 0


def test_a_stage_that_never_ran_is_an_input_error(tmp_path):
    (tmp_path / "r").mkdir()
    with pytest.raises(nd.DiagnosisInput):
        nd.diagnose(tmp_path / "r")
    assert nd.main([str(tmp_path / "r")]) == 2


def test_an_agent_that_never_searched_is_named(tmp_path):
    results, cache = _stage(tmp_path, tool_calls=0)
    assert "AGENT_DID_NOT_SEARCH" in codes(nd.diagnose(results, cache))


def test_queries_the_agent_reports_but_the_server_never_logged_are_named(tmp_path):
    results, cache = _stage(tmp_path, reported=("treg als", "an invented query"))
    data = nd.diagnose(results, cache)

    finding = next(f for f in data["findings"] if f["code"] == "QUERIES_NOT_IN_LOG")
    assert finding["missing"] == ["an invented query"] and nd.main([str(results), "--cache", str(cache)]) == 1


def test_a_work_cited_but_not_returned_by_this_stage_is_named(tmp_path):
    results, cache = _stage(tmp_path, cited=("https://openalex.org/W9",))
    assert "CITED_OUTSIDE_THIS_STAGE" in codes(nd.diagnose(results, cache))


def test_failed_and_exhausted_searches_are_problems(tmp_path):
    results, cache = _stage(tmp_path, outcome="failed")
    assert "SEARCH_FAILED" in codes(nd.diagnose(results, cache))


def test_an_unverified_entry_carries_the_hosts_reason(tmp_path):
    results, cache = _stage(tmp_path, entry_status="unverified")
    finding = next(f for f in nd.diagnose(results, cache)["findings"] if f["code"] == "ENTRY_UNVERIFIED")
    assert "never returned" in finding["detail"]


def test_thin_abstracts_are_a_note_not_a_problem(tmp_path):
    results, cache = _stage(tmp_path, works=[_w("W1", None), _w("W2", None)])
    data = nd.diagnose(results, cache)

    assert "THIN_ABSTRACTS" in codes(data) and data["problems"] == 0


def test_no_search_at_all_is_a_problem_and_the_tool_log_may_be_absent(tmp_path):
    results, cache = _stage(tmp_path)
    (results / nd.SERVER_LOG).write_text("")
    (results / nd.TOOL_LOG).unlink()
    data = nd.diagnose(results, cache)

    assert "NO_SEARCH" in codes(data) and data["entries"][0]["search_calls"] is None
    assert "absent" in nd.render(data)


def test_write_puts_both_files_in_the_archive(tmp_path):
    results, cache = _stage(tmp_path)
    nd.write(results, cache)
    assert json.loads((results / nd.OUT_JSON).read_text())["problems"] == 0
    assert "Novelty stage diagnosis" in (results / nd.OUT_MD).read_text()


def test_no_prior_art_found_without_an_answered_search_is_named(tmp_path):
    """The `pl-prod-013` H-2 case: every call was refused for budget and the verdict was still recorded."""
    results, cache = _stage(tmp_path, outcome="budget_spent")
    report = json.loads((results / nd.REPORT).read_text())
    report["entries"][0]["novelty"]["verdict"] = "no_prior_art_found"
    (results / nd.REPORT).write_text(json.dumps(report))

    assert "NO_PRIOR_ART_WITHOUT_A_SEARCH" in codes(nd.diagnose(results, cache))


def test_a_second_attempt_reads_only_its_own_lines_of_the_shared_server_log(tmp_path):
    results, cache = _stage(tmp_path)
    old = [{"event": "search", "query": "attempt one", "outcome": "budget_spent", "from_cache": False}] * 5
    log = results / nd.SERVER_LOG
    log.write_text("".join(json.dumps(r) + "\n" for r in old) + log.read_text())
    report = json.loads((results / nd.REPORT).read_text())
    report["server_log_offset"] = 5
    (results / nd.REPORT).write_text(json.dumps(report))

    data = nd.diagnose(results, cache)
    assert data["figures"]["searches_logged"] == 1 and "SEARCH_BUDGET_SPENT" not in codes(data)


def test_reads_and_paywalled_fetches_are_counted_as_figures_and_notes(tmp_path):
    results, cache = _stage(tmp_path)
    log = results / nd.SERVER_LOG
    log.write_text(
        log.read_text()
        + "".join(
            json.dumps(r) + "\n"
            for r in [
                {"event": "fetch", "outcome": "paywalled"},
                {"event": "fetch", "outcome": "fetched"},
                {"event": "read", "outcome": "read", "paper": "doi:10.1/a"},
                {"event": "read", "outcome": "read", "paper": "doi:10.1/a"},
                {"event": "read", "outcome": "budget_spent", "paper": "doi:10.1/b"},
            ]
        )
    )
    data = nd.diagnose(results, cache)

    assert data["figures"]["papers_opened"] == 1 and data["figures"]["read_outcomes"] == {"read": 2, "budget_spent": 1}
    assert data["figures"]["fetch_outcomes"] == {"fetched": 1, "paywalled": 1}
    assert {"READ_BUDGET_SPENT", "PAYWALLED_WHEN_FETCHED"} <= codes(data) and data["problems"] == 0


# --- the host's per-work decisions ------------------------------------------------------------------------------


def _prov(**over):
    block = {
        "kept": [],
        "dropped": [],
        "rationale_only": [],
        "works_listed": 0,
        "works_dropped": 0,
        "works_read_in_full": 0,
    }
    return block | over


def test_a_dropped_work_is_a_problem_and_names_the_id(tmp_path):
    results, cache = _stage(
        tmp_path,
        provenance=_prov(
            kept=[{"openalex_id": "https://openalex.org/W1", "evidence_depth": "abstract"}],
            works_listed=1,
            dropped=[
                {
                    "openalex_id": "W999",
                    "title": None,
                    "reason": "named in the rationale only, and the search " "tool never returned it",
                }
            ],
            works_dropped=1,
        ),
    )
    data = nd.diagnose(results, cache)

    finding = next(f for f in data["findings"] if f["code"] == "WORK_NOT_SERVED")
    assert finding["severity"] == "problem" and finding["works"] == ["W999"] and data["problems"] >= 1


def test_a_served_work_left_out_of_closest_works_is_a_note_only(tmp_path):
    results, cache = _stage(
        tmp_path,
        provenance=_prov(
            kept=[{"openalex_id": "https://openalex.org/W1", "evidence_depth": "abstract"}],
            works_listed=1,
            rationale_only=["W1"],
        ),
    )
    data = nd.diagnose(results, cache)

    finding = next(f for f in data["findings"] if f["code"] == "RATIONALE_WORK_UNLISTED")
    assert finding["severity"] == "note" and finding["works"] == ["W1"] and data["problems"] == 0


def test_a_verdict_left_with_no_served_work_is_named_as_such(tmp_path):
    results, cache = _stage(
        tmp_path,
        entry_status="unverified",
        provenance=_prov(
            dropped=[{"openalex_id": "W999", "title": "T", "reason": "the search tool never returned this work"}],
            works_dropped=1,
        ),
    )
    data = nd.diagnose(results, cache)

    assert {"VERDICT_ON_NO_SERVED_WORK", "WORK_NOT_SERVED", "ENTRY_UNVERIFIED"} <= codes(data)


def test_an_entry_refused_for_another_reason_is_not_read_as_a_provenance_failure(tmp_path):
    """It kept a work, so whatever refused it, it was not the host finding nothing checkable."""
    results, cache = _stage(
        tmp_path,
        entry_status="unverified",
        provenance=_prov(
            kept=[{"openalex_id": "https://openalex.org/W1", "evidence_depth": "full_text"}],
            works_listed=1,
            works_read_in_full=1,
        ),
    )
    data = nd.diagnose(results, cache)

    assert "VERDICT_ON_NO_SERVED_WORK" not in codes(data) and "ENTRY_UNVERIFIED" in codes(data)
    # A refused entry's block counts in the union too, exactly as it does in the three sums.
    assert data["figures"]["works_read_in_full"] == data["figures"]["distinct_works_read_in_full"] == 1


def test_the_work_figures_are_absent_not_zero_for_an_archive_without_the_block(tmp_path):
    results, cache = _stage(tmp_path, provenance=None)
    data = nd.diagnose(results, cache)

    assert data["figures"]["works_listed"] is None and data["figures"]["works_dropped"] is None
    assert data["figures"]["works_read_in_full"] is None
    assert data["figures"]["distinct_works_read_in_full"] is None
    assert data["entries"][0]["works_listed"] is None
    assert "| absent |" in nd.render(data)


def test_one_full_text_two_hypotheses_cite_is_summed_twice_and_counted_once(tmp_path):
    """The sum answers how deep each verdict goes, the union how many works the stage read: with one work read
    by both agents they disagree, and only the union is comparable with the log's `papers_opened`."""
    results, cache = _stage(tmp_path)
    shared = [{"openalex_id": "https://openalex.org/W1", "evidence_depth": "full_text"}]
    report = json.loads((results / nd.REPORT).read_text())
    report["entries"] = [
        {
            "hypothesis_id": hid,
            "status": "assessed",
            "novelty": report["entries"][0]["novelty"],
            "provenance": _prov(kept=shared, works_listed=1, works_read_in_full=1),
        }
        for hid in ("H-1", "H-2")
    ]
    (results / nd.REPORT).write_text(json.dumps(report))
    log = results / nd.SERVER_LOG
    log.write_text(log.read_text() + json.dumps({"event": "read", "outcome": "read", "paper": "doi:10.1/a"}) + "\n")
    data = nd.diagnose(results, cache)

    assert data["figures"]["works_read_in_full"] == 2
    assert data["figures"]["distinct_works_read_in_full"] == data["figures"]["papers_opened"] == 1
    # A gap between the two is normal arithmetic, not a defect, so it raises no provenance finding.
    assert not {"WORK_NOT_SERVED", "VERDICT_ON_NO_SERVED_WORK"} & codes(data)
    assert "distinct_works_read_in_full" in nd.render(data)


def test_the_per_hypothesis_table_carries_the_work_counts(tmp_path):
    results, cache = _stage(tmp_path)
    assert "works kept/dropped/read in full" in nd.render(nd.diagnose(results, cache))
    assert "| 1/0/0 |" in nd.render(nd.diagnose(results, cache))
