"""A bounded, preregistered diagnostic of predicted binding across the local HLA allele panel.

This measures coverage of the alleles represented in the cache. It does not estimate population
coverage, antigen presentation, immunogenicity, or a treatment effect.
"""

from __future__ import annotations

from collections import Counter
from itertools import combinations
from pathlib import Path
from typing import Any

from .a4_hla_binding import BINDER_RANK, allele_name, read_cache
from .datasets import load_table1, load_table2

ANALYSIS_ID = "hla_panel_compact_v1"
MAX_PEPTIDES = 8


def run(table1: Path, table2: Path, cache: Path) -> dict[str, Any]:
    peptides = sorted({row.sequence for row in load_table1(table1)})
    hla_rows = load_table2(table2)
    if not hla_rows:
        raise ValueError("HLA table has no rows")
    fields = list(hla_rows[0])
    if len(fields) < 2:
        raise ValueError("HLA table has no allele column")
    alleles = sorted({allele_name(str(row[fields[1]])) for row in hla_rows})
    if not peptides or not alleles:
        raise ValueError("empty peptide or HLA allele panel")
    rows = read_cache(cache)
    missing = [(p, a) for p in peptides for a in alleles if (a, p) not in rows]
    if missing:
        raise ValueError(f"prediction grid is incomplete: {len(missing)} missing pairs")

    masks = []
    for peptide in peptides:
        mask = 0
        for bit, allele in enumerate(alleles):
            rank = float(rows[(allele, peptide)]["rank"])
            if not 0 <= rank <= 100:
                raise ValueError(f"invalid percentile rank for {peptide}, {allele}: {rank}")
            if rank <= BINDER_RANK:
                mask |= 1 << bit
        masks.append(mask)

    full = (1 << len(alleles)) - 1
    selected = None
    selected_mask = 0
    for size in range(1, min(MAX_PEPTIDES, len(peptides)) + 1):
        for indexes in combinations(range(len(peptides)), size):
            combined = 0
            for index in indexes:
                combined |= masks[index]
            if combined == full:
                selected, selected_mask = indexes, combined
                break
        if selected is not None:
            break

    if selected is None:
        size = min(MAX_PEPTIDES, len(peptides))
        # Choose the lexicographically first subset at the maximal observed coverage.
        for indexes in combinations(range(len(peptides)), size):
            combined = 0
            for index in indexes:
                combined |= masks[index]
            if selected is None or combined.bit_count() > selected_mask.bit_count():
                selected, selected_mask = indexes, combined

    assert selected is not None
    control = Counter()
    for indexes in combinations(range(len(peptides)), len(selected)):
        combined = 0
        for index in indexes:
            combined |= masks[index]
        control[combined.bit_count()] += 1
    total = sum(control.values())
    covered = [allele for bit, allele in enumerate(alleles) if selected_mask & (1 << bit)]
    met = selected_mask == full
    return {
        "analysis_id": ANALYSIS_ID,
        "rule": {
            "binder_percentile_rank_lte": BINDER_RANK,
            "maximum_pool_size": MAX_PEPTIDES,
            "selection": "smallest full-cover subset, then lexicographic; otherwise maximal coverage at size cap",
            "control": "all size-matched subsets of the same recognized peptides",
        },
        "peptides_in_panel": len(peptides),
        "alleles": alleles,
        "pairs_checked": len(peptides) * len(alleles),
        "selected_peptides": [peptides[i] for i in selected],
        "selected_covered_alleles": covered,
        "covered_alleles": len(covered),
        "control_coverage_counts": {str(k): control[k] for k in sorted(control)},
        "control_subsets": total,
        "control_full_coverage_fraction": control[len(alleles)] / total,
        "outcome": "panel_condition_met" if met else "panel_condition_failed",
        "negative_result": not met,
        "conclusion": (
            "A pool of at most eight peptides covers all alleles in the cached four-allele panel at the "
            "preregistered predicted-binding threshold."
            if met
            else "No pool of at most eight peptides covers every allele in the cached panel at the "
            "preregistered predicted-binding threshold."
        ),
        "limitations": [
            "Panel coverage is not population coverage; allele frequencies are unavailable.",
            "A predicted binder is not evidence of antigen presentation or immunogenicity.",
            "The control draws from recognized peptides, not random peptides from the C9orf72 region.",
        ],
        "results": [
            {
                "key": "selected_pool_size",
                "value": len(selected),
                "n": len(peptides),
                "test": "exhaustive subset search",
            },
            {
                "key": "covered_alleles",
                "value": len(covered),
                "n": len(alleles),
                "test": f"predicted percentile rank <= {BINDER_RANK}",
            },
            {
                "key": "size_matched_full_coverage_fraction",
                "value": control[len(alleles)] / total,
                "n": total,
                "test": "exhaustive size-matched subset control",
            },
        ],
    }
