"""A4: MHC-II binding predictions read from the cache and checked against the paper's own table. Offline."""

from __future__ import annotations

import json
from pathlib import Path

from analysis.a4_hla_binding import BINDER_RANK, DQ_ALPHA, allele_name, read_cache, run
from analysis.stats import spearman

T1_HEADER = (
    '"**Peptide**\n**residue**",**Peptide sequence**,"**Response**\n**frequency (%)**",'
    '"**Average**\n**magnitude**"\n'
)
T2_HEADER = '**Peptide sequence**,**Allele**,"**RATE**\n**_P_ value**","**Binding percentile**\n**rank**"\n'


def tables(tmp_path: Path, peptides, hla) -> tuple[Path, Path]:
    t1 = tmp_path / "t1.csv"
    t1.write_text(
        T1_HEADER + "".join(f"{start}–{start + 14},{seq},{k}/{n} ({pct}%),100\n" for start, seq, k, n, pct in peptides)
    )
    t2 = tmp_path / "t2.csv"
    t2.write_text(T2_HEADER + "".join(f"{seq},{allele},0.02,{rank}\n" for seq, allele, rank in hla))
    return t1, t2


def cache(tmp_path: Path, rows) -> Path:
    path = tmp_path / "cache.jsonl"
    path.write_text(
        "".join(
            json.dumps(
                {
                    "key": f"netmhciipan_el|15|{allele}|{peptide}",
                    "row": {
                        "allele": allele,
                        "peptide": peptide,
                        "core_peptide": peptide[:9],
                        "length": "15",
                        "score": "0.1",
                        "rank": str(rank),
                    },
                }
            )
            + "\n"
            for allele, peptide, rank in rows
        )
    )
    return path


A, B = "AAAAAAAAAAAAAAA", "BBBBBBBBBBBBBBB"
DRB = "HLA-DRB1*03:01"
DQ = f"{DQ_ALPHA}/DQB1*05:01"


def test_a_dq_allele_is_named_with_its_alpha_chain_because_the_predictor_requires_one():
    """Table 2 names `DQB1*05:01` alone, which the API rejects. The assumed partner is tested, not asserted."""
    assert allele_name("DQB1*05:01") == DQ
    assert allele_name("DRB1*03:01") == "HLA-DRB1*03:01"
    assert allele_name("HLA-DRB1*03:01") == "HLA-DRB1*03:01"


def test_the_paper_s_own_ranks_are_the_internal_control(tmp_path):
    t1, t2 = tables(tmp_path, [(1, A, 7, 22, 32), (20, B, 3, 22, 14)], [(A, "DRB1*03:01", 4.9), (B, "DQB1*05:01", 12)])
    rows = cache(tmp_path, [(DRB, A, 4.9), (DRB, B, 50.0), (DQ, A, 80.0), (DQ, B, 12.0)])

    summary = run(t1, t2, rows)

    assert [r["match"] for r in summary["reproduction"]] == [True, True]
    assert summary["results"][0] == {
        "key": "paper_ranks_reproduced",
        "value": 2,
        "n": 2,
        "test": "equal to the printed precision, from the cache",
    }


def test_a_rank_that_does_not_reproduce_is_reported_as_such(tmp_path):
    """A mismatch is a finding about the published table, not something to smooth over."""
    t1, t2 = tables(tmp_path, [(1, A, 7, 22, 32)], [(A, "DRB1*03:01", 4.9)])
    rows = cache(tmp_path, [(DRB, A, 31.0), (DQ, A, 5.0)])

    summary = run(t1, t2, rows)

    assert summary["reproduction"][0]["match"] is False
    assert summary["reproduction"][0]["our_rank"] == 31.0 and summary["reproduction"][0]["paper_rank"] == 4.9
    assert summary["results"][0]["value"] == 0


def test_a_binder_is_counted_once_per_peptide_on_its_best_allele(tmp_path):
    t1, t2 = tables(tmp_path, [(1, A, 7, 22, 32), (20, B, 3, 22, 14)], [(A, "DRB1*03:01", 4.9)])
    rows = cache(tmp_path, [(DRB, A, 4.9), (DRB, B, 60.0)])

    summary = run(t1, t2, rows)

    assert summary["binders"] == [A]  # B passes on no allele
    assert summary["best_per_peptide"][A] == {"allele": DRB, "rank": 4.9}
    assert summary["results"][1]["value"] == 1 and summary["results"][1]["n"] == 2
    assert summary["results"][2]["value"] == 4.9  # the strongest rank over the grid
    assert BINDER_RANK == 10.0


def test_a_pair_absent_from_the_cache_is_named_and_not_imputed(tmp_path):
    t1, t2 = tables(tmp_path, [(1, A, 7, 22, 32), (20, B, 3, 22, 14)], [(A, "DRB1*03:01", 4.9)])
    rows = cache(tmp_path, [(DRB, A, 4.9)])

    summary = run(t1, t2, rows)

    assert summary["missing_pairs"] == [{"peptide": B, "allele": DRB}]
    assert summary["pairs"] == 1 and B not in summary["best_per_peptide"]


def test_the_assumption_is_in_the_summary_not_in_a_comment(tmp_path):
    t1, t2 = tables(tmp_path, [(1, A, 7, 22, 32)], [(A, "DQB1*05:01", 12)])
    summary = run(t1, t2, cache(tmp_path, [(DQ, A, 12.0)]))

    assert any("DQA1*01:01" in a and "assumed and tested" in a for a in summary["assumptions"])


def test_a_weak_correlation_is_a_negative_result_and_says_what_it_does_not_show(tmp_path):
    """The one number here that could speak about the biology. Weak is the expected answer, and it is reported
    as a negative result rather than left for a reader to infer."""
    peptides = [(1 + 20 * i, chr(65 + i) * 15, k, 22, 0) for i, k in enumerate([1, 2, 3, 4, 5, 6])]
    t1, t2 = tables(tmp_path, peptides, [(peptides[0][1], "DRB1*03:01", 30.0)])
    unordered = [30.0, 5.0, 40.0, 8.0, 35.0, 12.0]  # responses rise, ranks do not follow
    rows = cache(tmp_path, [(DRB, seq, unordered[i]) for i, (_, seq, *_) in enumerate(peptides)])

    summary = run(t1, t2, rows)

    assert summary["results"][3]["key"] == "spearman_response_vs_best_rank"
    assert summary["results"][3]["value"] == 0.086  # |rho| < 0.4, the threshold the analysis uses
    assert summary["negative_result"] is True
    assert "does not account for which peptides were immunodominant" in summary["conclusion"]


def test_a_strong_correlation_is_not_reported_as_a_result_this_analysis_can_settle(tmp_path):
    """The other branch, and the one that would matter scientifically: it is flagged for scrutiny, not claimed."""
    peptides = [(1 + 20 * i, chr(65 + i) * 15, k, 22, 0) for i, k in enumerate([1, 2, 3, 4, 5, 6])]
    t1, t2 = tables(tmp_path, peptides, [(peptides[0][1], "DRB1*03:01", 60.0)])
    rows = cache(tmp_path, [(DRB, seq, 60.0 - 8.0 * i) for i, (_, seq, *_) in enumerate(peptides)])

    summary = run(t1, t2, rows)

    assert summary["results"][3]["value"] == -1.0
    assert summary["negative_result"] is False
    assert "needs its own scrutiny" in summary["conclusion"]


def test_the_cache_reader_lets_a_later_record_win(tmp_path):
    path = tmp_path / "c.jsonl"
    path.write_text(
        json.dumps(
            {"key": "k", "row": {"allele": DRB, "peptide": A, "rank": "9.0", "score": "0.1", "core_peptide": A[:9]}}
        )
        + "\n"
        + json.dumps(
            {"key": "k", "row": {"allele": DRB, "peptide": A, "rank": "4.0", "score": "0.2", "core_peptide": A[:9]}}
        )
        + "\n"
    )

    assert read_cache(path)[(DRB, A)]["rank"] == "4.0"


def test_spearman_handles_ties_and_refuses_a_constant():
    assert spearman([1, 2, 3, 4, 5], [1, 2, 3, 4, 5]) == 1.0
    assert spearman([1, 2, 3, 4, 5], [5, 4, 3, 2, 1]) == -1.0
    assert spearman([1, 1, 2, 3, 3], [2, 2, 3, 4, 4]) == 1.0  # average ranks, not the 6*sum(d^2) shortcut
    assert spearman([1, 1, 1, 1], [1, 2, 3, 4]) is None  # undefined, not zero
    assert spearman([1, 2], [2, 1]) is None
