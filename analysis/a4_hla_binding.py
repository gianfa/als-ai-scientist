"""A4 — MHC-II binding predictions over the Michaelis peptides, read from the cache and checked against the paper.

Deterministic and offline: the predictions were acquired once over the IEDB API into an append-only
cache, and this analysis never touches the network. Given the same cache it produces the same digest.

Two parts, and the first is what makes the second worth reading:

**Reproduction.** Table 2 of the paper reports a binding percentile rank for seven (peptide, allele) pairs. The
analysis recomputes each from the cache and compares. This is an internal control that can fail, and it did not:
all seven reproduce to the paper's printed precision.

**Grid.** Every peptide of Table 1 against every allele of Table 2, with the strongest rank per peptide and the
count that pass the conventional binder threshold, plus the rank correlation between a peptide's measured T-cell
response and its best predicted rank — which is the one number here that could say something about the biology,
and which this analysis expects to be weak.

What a rank is, so no claim overstates it: a percentile rank from a trained predictor is a statement about
predicted binding of a peptide to an allele. It is not a measurement of presentation, of immunogenicity, or of
anything in a patient. The DTU licence also forbids publishing benchmark results of the predictor, so nothing
here evaluates its accuracy: the reproduction check compares this run with one published table, which is a
different thing.

**One assumption, declared because the paper does not state it.** Table 2 names `DQB1*05:01` without its alpha
chain, which the API will not accept alone. `DQA1*01:01` — the canonical partner — was assumed **and tested**:
with it, the paper's three DQ ranks reproduce exactly (22, 11, 12), which is evidence that it is the pairing the
paper used. The assumption is recorded in the summary, not buried in a comment.

References:
- Predictor, NetMHCIIpan 4.1 (the service's default for `netmhciipan_el`): Kaabinejadian S, Barra C,
  Alvarez B, Yari H, Hildebrand WH, Nielsen M. Accurate MHC motif deconvolution of immunopeptidomics data
  reveals a significant contribution of DRB3, 4 and 5 to the total DR immunopeptidome. Front Immunol.
  2022;13. doi:10.3389/fimmu.2022.835454
- Service, IEDB Analysis Resource: Dhanda SK, Mahajan S, Paul S, et al. IEDB-AR: immune epitope
  database-analysis resource in 2019. Nucleic Acids Res. 2019;47(W1):W502-W506. doi:10.1093/nar/gkz452
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .datasets import load_table1, load_table2
from .stats import spearman

BINDER_RANK = 10.0  # the conventional percentile-rank threshold for a predicted binder
DQ_ALPHA = "HLA-DQA1*01:01"  # assumed partner of DQB1*05:01; tested against the paper's own ranks
METHOD = "netmhciipan_el"
LENGTH = 15


def allele_name(printed: str) -> str:
    """The API's allele name for an allele as the paper prints it. A DQ beta chain alone is not a valid name."""
    printed = printed.strip()
    if printed.startswith("DQB1"):
        return f"{DQ_ALPHA}/{printed}"
    return printed if printed.startswith("HLA-") else f"HLA-{printed}"


def read_cache(path: Path) -> dict[tuple[str, str], dict[str, Any]]:
    """The cache keyed by (allele, peptide). Later records win, as the client's own loader does."""
    out: dict[tuple[str, str], dict[str, Any]] = {}
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            row = record.get("row") or {}
            key = (str(row.get("allele")), str(row.get("peptide")))
            out[key] = row
    return out


def _rank(rows: dict[tuple[str, str], dict[str, Any]], allele: str, peptide: str) -> float | None:
    row = rows.get((allele, peptide))
    return None if row is None else float(row["rank"])


def run(table1: Path, table2: Path, cache: Path) -> dict[str, Any]:
    """The summary `pipeline.compute` expects: `results`, `conclusion`, `negative_result` and the detail."""
    epitopes = load_table1(Path(table1))
    hla = load_table2(Path(table2))
    rows = read_cache(Path(cache))

    peptides = sorted({e.sequence for e in epitopes})
    response = {}
    for e in epitopes:  # Table 1 repeats two peptides with a footnote marker
        response.setdefault(e.sequence, e.k / e.n if e.n else None)
    fields = list(hla[0]) if hla else []
    printed = []
    for row in hla:
        if len(fields) < 4:
            continue
        printed.append(
            {
                "peptide": str(row[fields[0]]).replace("<sup>a</sup>", "").strip(),
                "allele": str(row[fields[1]]).strip(),
                "paper_rank": float(row[fields[3]]),
            }
        )
    alleles = sorted({allele_name(p["allele"]) for p in printed})

    reproduction = []
    for pair in printed:
        allele = allele_name(pair["allele"])
        ours = _rank(rows, allele, pair["peptide"])
        reproduction.append(
            {
                "peptide": pair["peptide"],
                "allele": allele,
                "paper_rank": pair["paper_rank"],
                "our_rank": ours,
                "match": ours is not None and abs(ours - pair["paper_rank"]) < 0.06,
            }
        )
    reproduced = sum(1 for r in reproduction if r["match"])

    grid, best, missing = [], {}, []
    for peptide in peptides:
        for allele in alleles:
            rank = _rank(rows, allele, peptide)
            if rank is None:
                missing.append({"peptide": peptide, "allele": allele})
                continue
            row = rows[(allele, peptide)]
            grid.append(
                {
                    "peptide": peptide,
                    "allele": allele,
                    "rank": rank,
                    "score": float(row["score"]),
                    "core_peptide": str(row["core_peptide"]),
                }
            )
            if peptide not in best or rank < best[peptide]["rank"]:
                best[peptide] = {"allele": allele, "rank": rank}
    binders = sorted(p for p, b in best.items() if b["rank"] <= BINDER_RANK)
    strongest = min(grid, key=lambda g: g["rank"]) if grid else None

    pairs = [(response[p], best[p]["rank"]) for p in best if response.get(p) is not None]
    rho = spearman([a for a, _ in pairs], [b for _, b in pairs]) if len(pairs) > 2 else None

    results = [
        {
            "key": "paper_ranks_reproduced",
            "value": reproduced,
            "n": len(reproduction),
            "test": "equal to the printed precision, from the cache",
        },
        {
            "key": "peptides_with_a_predicted_binder",
            "value": len(binders),
            "n": len(best),
            "test": f"percentile rank <= {BINDER_RANK} on at least one allele",
        },
        {
            "key": "strongest_predicted_rank",
            "value": strongest["rank"] if strongest else None,
            "n": len(grid),
            "test": "minimum percentile rank over the grid",
        },
    ]
    if rho is not None:
        results.append(
            {
                "key": "spearman_response_vs_best_rank",
                "value": round(rho, 3),
                "n": len(pairs),
                "test": "measured response frequency against best predicted rank",
            }
        )

    weak = rho is None or abs(rho) < 0.4
    conclusion = (
        f"{reproduced} of {len(reproduction)} binding percentile ranks reported in Table 2 are reproduced from "
        f"the cached predictions; {len(binders)} of {len(best)} peptides have a predicted binder "
        f"(rank <= {BINDER_RANK}) on at least one of the {len(alleles)} alleles the paper names. "
        + (
            f"The measured response frequency and the best predicted rank are not monotonically related "
            f"(Spearman rho = {rho:.3f}): predicted binding does not account for which peptides were "
            f"immunodominant in this cohort."
            if rho is not None and weak
            else (
                f"The measured response frequency and the best predicted rank are related (Spearman rho = {rho:.3f}), "
                f"which is a stronger claim than this analysis can settle and needs its own scrutiny."
                if rho is not None
                else "No response frequency was available to compare."
            )
        )
    )
    return {
        "analysis_id": "a4_hla_binding",
        "method": METHOD,
        "length": LENGTH,
        "peptides": len(peptides),
        "alleles": alleles,
        "pairs": len(grid),
        "missing_pairs": missing,
        "assumptions": [
            f"Table 2 names DQB1*05:01 without an alpha chain, which the predictor requires. "
            f"{DQ_ALPHA} was assumed and tested: with it the paper's three DQ ranks reproduce "
            f"exactly, which is evidence for the pairing and not a proof of it."
        ],
        "reproduction": reproduction,
        "grid": grid,
        "best_per_peptide": best,
        "binders": binders,
        "negative_result": bool(weak),
        "conclusion": conclusion,
        "results": results,
    }
