"""Append-only storage and the single writer of the knowledge base."""

from __future__ import annotations

import multiprocessing
from pathlib import Path

import pytest
from kb_factories import NOW, claim, decision, entity, experiment, hypothesis, ignorance, intervention, relation

from knowledge_base.schemas import Claim, Event
from knowledge_base.store import KnowledgeBase, KnowledgeBaseError, read_records


def opened(root: Path):
    return KnowledgeBase.open(root, clock=lambda: NOW)


def events(root: Path) -> list[Event]:
    return [r for r in read_records(root).values() if isinstance(r, Event)]


def test_commit_writes_records_and_one_written_event_each(tmp_path):
    with opened(tmp_path) as kb:
        result = kb.commit(
            [
                hypothesis(id="H-1"),
                claim(id="C-1"),
                relation(id="L-1", type="part_of", source_id="C-1", target_id="H-1"),
            ],
            run_id="RUN-1",
        )
    assert result.written == ["H-1", "C-1", "L-1"] and not result.rejected and not result.seen_again
    stored = read_records(tmp_path)
    assert {"H-1", "C-1", "L-1"} <= set(stored)
    assert [(e.event, e.record_id) for e in events(tmp_path)] == [
        ("written", "H-1"),
        ("written", "C-1"),
        ("written", "L-1"),
    ]
    assert (tmp_path / "claims.jsonl").read_text().count("\n") == 1


def test_records_survive_reopening_and_ids_continue(tmp_path):
    with opened(tmp_path) as kb:
        kb.commit([claim(id=kb.allocate("claim"))], run_id="RUN-1")
    with opened(tmp_path) as kb:
        assert "C-1" in kb.records
        assert kb.allocate("claim") == "C-2"
        assert kb.allocate("event") == "V-2"  # V-1 is the written event of the first run


def test_duplicate_claim_across_runs_is_seen_again_not_rewritten(tmp_path):
    with opened(tmp_path) as kb:
        kb.commit([hypothesis(id="H-1"), claim(id="C-1")], run_id="RUN-1")
    with opened(tmp_path) as kb:
        result = kb.commit(
            [
                claim(id="C-7", content=" DPRs are DETECTED in c9-als spinal cord "),
                relation(id="L-1", type="part_of", source_id="C-7", target_id="H-1"),
            ],
            run_id="RUN-2",
        )
    assert result.seen_again == {"C-7": "C-1"}
    assert result.written == ["L-1"]
    stored = read_records(tmp_path)
    assert "C-7" not in stored
    assert stored["L-1"].source_id == "C-1"  # redirected to the existing claim
    assert len([r for r in stored.values() if isinstance(r, Claim)]) == 1
    seen = [e for e in events(tmp_path) if e.event == "seen_again"]
    assert [(e.record_id, e.run_id) for e in seen] == [("C-1", "RUN-2")]


def test_redirect_that_creates_a_self_loop_is_rejected(tmp_path):
    with opened(tmp_path) as kb:
        kb.commit([claim(id="C-1")], run_id="RUN-1")
        result = kb.commit(
            [
                claim(id="C-2", content=claim().content.upper()),
                relation(
                    id="L-1",
                    type="derived_from",
                    source_id="C-2",
                    target_id="C-1",
                    justification_method="agent_inference",
                    provenance_status="unverified",
                ),
            ],
            run_id="RUN-1",
        )
    assert "L-1" in result.rejected and "itself" in result.rejected["L-1"][0]


def test_invalid_record_is_rejected_with_reasons_and_dependants_fail(tmp_path):
    with opened(tmp_path) as kb:
        result = kb.commit(
            [
                ignorance(id="I-1", hypothesis_id="H-404"),
                relation(
                    id="L-1",
                    type="questions",
                    source_id="I-1",
                    target_id="C-1",
                    justification_method="agent_inference",
                    provenance_status="unverified",
                    source_refs=[],
                ),
            ],
            run_id="RUN-1",
        )
    assert result.written == []
    assert result.rejected["I-1"] == ["ignorance I-1 refers to unknown H-404"]
    assert any("unknown I-1" in r for r in result.rejected["L-1"])
    rejected = [e for e in events(tmp_path) if e.event == "rejected"]
    assert [e.record_id for e in rejected] == ["I-1", "L-1"] and "H-404" in rejected[0].detail
    assert "I-1" not in read_records(tmp_path)


def test_an_entity_registered_in_the_batch_can_be_referenced_later_in_it(tmp_path):
    with opened(tmp_path) as kb:
        result = kb.commit([entity(id="N-1"), claim(id="C-1", entity_refs=["N-1"])], run_id="RUN-1")
    assert result.written == ["N-1", "C-1"]


def _try_open(root: str, queue) -> None:
    try:
        with KnowledgeBase.open(Path(root)):
            queue.put("opened")
    except KnowledgeBaseError as err:
        queue.put(str(err))


def test_a_second_writer_fails_at_once(tmp_path):
    with opened(tmp_path):
        queue = multiprocessing.get_context("fork").Queue()
        process = multiprocessing.get_context("fork").Process(target=_try_open, args=(str(tmp_path), queue))
        process.start()
        process.join(10)
        assert "already open for writing" in queue.get(timeout=5)


def test_reader_skips_a_partial_last_line_but_rejects_corruption(tmp_path):
    with opened(tmp_path) as kb:
        kb.commit([claim(id="C-1")], run_id="RUN-1")
    path = tmp_path / "claims.jsonl"
    path.write_text(path.read_text() + '{"id": "C-2", "type": "obs')  # a write in progress
    assert set(r for r in read_records(tmp_path) if r.startswith("C-")) == {"C-1"}
    path.write_text(path.read_text() + '\n{"not": "a claim"}\n')
    with pytest.raises(KnowledgeBaseError, match="claims.jsonl:2"):
        read_records(tmp_path)


def test_reader_refuses_duplicate_ids(tmp_path):
    with opened(tmp_path) as kb:
        kb.commit([claim(id="C-1")], run_id="RUN-1")
    path = tmp_path / "claims.jsonl"
    path.write_text(path.read_text() * 2)
    with pytest.raises(KnowledgeBaseError, match="duplicate id C-1"):
        read_records(tmp_path)


def test_files_are_only_appended(tmp_path):
    with opened(tmp_path) as kb:
        kb.commit([claim(id="C-1")], run_id="RUN-1")
        first = (tmp_path / "claims.jsonl").read_text()
        kb.commit([claim(id="C-2", content="Another finding.")], run_id="RUN-1")
    assert (tmp_path / "claims.jsonl").read_text().startswith(first)


def test_experiments_and_decisions_are_stored_and_tested_claims_linked(tmp_path):
    with opened(tmp_path) as kb:
        result = kb.commit(
            [
                hypothesis(id="H-1"),
                claim(id="C-1"),
                experiment(),
                relation(
                    id="L-1",
                    type="tests",
                    source_id="EXP-1",
                    target_id="C-1",
                    justification_method="agent_inference",
                    provenance_status="unverified",
                    source_refs=[],
                ),
                decision(),
            ],
            run_id="RUN-1",
        )
        assert kb.allocate("experiment") == "EXP-2" and kb.allocate("decision") == "D-2"
    assert result.written == ["H-1", "C-1", "EXP-1", "L-1", "D-1"]
    assert (tmp_path / "experiments.jsonl").is_file() and (tmp_path / "decisions.jsonl").is_file()
    assert {"EXP-1", "D-1"} <= set(read_records(tmp_path))


def test_interventions_are_stored_and_allocated(tmp_path):
    with opened(tmp_path) as kb:
        result = kb.commit([hypothesis(id="H-1"), claim(id="C-1"), intervention()], run_id="RUN-1")
        assert kb.allocate("intervention") == "INT-2"
    assert result.written == ["H-1", "C-1", "INT-1"]
    assert (tmp_path / "interventions.jsonl").is_file() and "INT-1" in read_records(tmp_path)


# --- canonicalising a claim's source references --------------------------------------------------
# The host is the single writer, so it is the host that puts an agent's references into one form. These
# cover the function it uses; `tests/test_pipeline_proposals.py` covers that it is actually called.


def test_canonicalise_refs_puts_one_source_into_one_form() -> None:
    from tools.literature.store import canonicalise_refs

    canonical, unresolved = canonicalise_refs(["10.1038/S41586-025-09588-6", "PMID:29542041", "PMC11247723"])
    assert canonical == ["doi:10.1038/s41586-025-09588-6", "pmid:29542041", "pmcid:PMC11247723"]
    assert unresolved == []


def test_canonicalise_refs_keeps_an_internal_reference_untouched() -> None:
    """A receipt, a dataset version, an experiment or a result is provenance of a different kind;
    asking whether it is a DOI is a category error."""
    from tools.literature.store import canonicalise_refs

    refs = ["receipt:abc", "dataset:michaelis-table1@89af13", "experiment:EXP-72@eb2a25", "result:a4@b629"]
    assert canonicalise_refs(refs) == (refs, [])


def test_canonicalise_refs_keeps_what_it_cannot_resolve_and_names_it() -> None:
    """Dropping it would hide that the claim rests on nothing checkable. All three strings below are
    real `source_refs` from production runs."""
    from tools.literature.store import canonicalise_refs, unresolved_refs

    refs = ["Web search results", "C-111", "alsnewstoday.com (ALS News Today)"]
    canonical, unresolved = canonicalise_refs(refs)
    assert canonical == refs  # nothing is lost
    assert unresolved == refs  # and nothing is pretended
    assert unresolved_refs(refs) == refs


def test_a_declared_persistent_identifier_is_not_unresolvable_even_when_the_store_cannot_index_it() -> None:
    """`is_declared_source` declares eight schemes valid; `canonical_id` maps three of them, because those are
    the three the store indexes. Reading the two as one predicate meant the proposal gate would flag a correct
    arXiv citation as `cites_an_unresolvable_source` while `SOURCE_FORM_HELP` told agents to write exactly
    that. Measured when this was fixed: 0 such references on record across 1371, so it was a trap, not a
    wound."""
    from tools.literature.store import canonicalise_refs, is_declared_source, unresolved_refs

    refs = ["arxiv:2401.01234", "isbn:9780262035613", "handle:123/4", "accession:GSE12345"]
    canonical, unresolved = canonicalise_refs(refs)
    assert all(is_declared_source(r) for r in refs)
    assert canonical == refs, "kept exactly as written: canonical_id maps none of them"
    assert unresolved == [] and unresolved_refs(refs) == []


def test_a_url_is_a_location_and_stays_unresolvable_however_it_is_declared() -> None:
    """The one scheme of `SOURCE_SCHEMES` left out of `IDENTIFIER_SCHEMES`, deliberately: `C-87` rested on a
    university press page, and `url:` must not launder it. All 16 declared-but-unresolvable references in the
    archives when this was fixed were bare URLs, and all 16 stay flagged."""
    from tools.literature.store import unresolved_refs

    for ref in [
        "https://news.uzh.ch/en/articles/media/2026/multiple-sclerosis-autoimmune-response.html",
        "url:https://news.uzh.ch/en/articles/x.html",
        "https://zenodo.org/records/123",
        # Deliberate, not an oversight: an arXiv *page* is a location. The agent is told to write
        # `arxiv:2401.01234`, and `canonical_id` stays narrow so the fetcher is never handed a URL.
        "https://arxiv.org/abs/2401.01234",
    ]:
        assert unresolved_refs([ref]) == [ref], ref


def test_the_identifier_schemes_are_the_declared_ones_minus_url() -> None:
    """Stated as an equation so a scheme added to one list cannot be forgotten in the other."""
    from tools.literature.store import IDENTIFIER_SCHEMES, SOURCE_SCHEMES

    assert set(SOURCE_SCHEMES) - set(IDENTIFIER_SCHEMES) == {"url"}
    assert set(IDENTIFIER_SCHEMES) < set(SOURCE_SCHEMES)


def test_canonicalise_refs_preserves_order_and_drops_only_blanks() -> None:
    from tools.literature.store import canonicalise_refs

    canonical, _ = canonicalise_refs(["PMID:29542041", "", "   ", "PMID:41034581"])
    assert canonical == ["pmid:29542041", "pmid:41034581"]
