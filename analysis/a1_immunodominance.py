import random
from fractions import Fraction

from .stats import clopper_pearson, fisher_exact_2x2, holm


def _f(value):
    return round(float(value), 8)


def run(rows, seed=20260928, bootstrap_replicates=10000):
    unique = [row for row in rows if not row.phospho]
    top = next((row for row in unique if row.start == 56), None)
    if top is None:
        raise ValueError("table1 has no primary 56-70 epitope")
    comparisons = []
    p_values = []
    for row in unique:
        if row is top:
            continue
        p = fisher_exact_2x2(top.k, top.n - top.k, row.k, row.n - row.k)
        p_values.append(p)
        comparisons.append({"epitope": row.residue_range, "k": row.k, "n": row.n, "p_value": _f(p)})
    for item, adjusted in zip(comparisons, holm(p_values)):
        item["adjusted_p"] = _f(adjusted)
        item["reject_alpha_0_05"] = adjusted <= Fraction(1, 20)

    rng = random.Random(seed)
    rank_one = 0
    for _ in range(bootstrap_replicates):
        # Use the same N-specific binomial draw, without introducing numpy.
        draws = [(sum(rng.random() < row.k / row.n for _ in range(row.n)) / row.n, row) for row in unique]
        if top is max(draws, key=lambda pair: (pair[0], -pair[1].start))[1]:
            rank_one += 1
    denominator_audit = [
        {
            "epitope": r.residue_range,
            "k": r.k,
            "n": r.n,
            "cohort_n": 28,
            "unassayed_donors": 28 - r.n,
            "printed_pct_recomputed": round(100 * r.k / r.n) == r.printed_pct,
        }
        for r in rows
    ]
    return {
        "analysis_id": "a1_immunodominance",
        "method": "Exact arithmetic verification; Clopper-Pearson 95% intervals; two-sided Fisher exact tests with Holm correction; seeded binomial rank bootstrap.",
        "seed": seed,
        "table1_rows": [
            {
                "epitope": r.residue_range,
                "sequence": r.sequence,
                "k": r.k,
                "n": r.n,
                "frequency": _f(Fraction(r.k, r.n)),
                "ci_low": _f(clopper_pearson(r.k, r.n)[0]),
                "ci_high": _f(clopper_pearson(r.k, r.n)[1]),
                "phospho": r.phospho,
            }
            for r in rows
        ],
        "denominator_audit": denominator_audit,
        "pairwise_fisher": comparisons,
        "rank_stability": {
            "epitope": top.residue_range,
            "replicates": bootstrap_replicates,
            "rank_one_count": rank_one,
            "rank_one_probability": _f(Fraction(rank_one, bootstrap_replicates)),
        },
        "negative_result": not any(x["reject_alpha_0_05"] for x in comparisons),
        "conclusion": "Table 1 alone does not identify a single immunodominant C9orf72 epitope; any regional signal remains non-causal and donor-pairing is unavailable.",
        "limitations": [
            "Printed k/N values are treated as counts.",
            "Donors may recur across peptide comparisons; Holm is conservative but dependence is unquantified.",
            "The table denominators (19-25) are below the reported 28-donor cohort.",
            "This analysis cannot establish mechanism, HLA causality, efficacy, or treatment advice.",
        ],
    }
