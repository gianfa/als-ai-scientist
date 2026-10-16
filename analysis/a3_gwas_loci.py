import gzip
import statistics
from array import array

# Windows are our own choice for a descriptive look, not a standard locus definition.
REGIONS = {
    "9p21_26.5-28.5Mb_GRCh37": ("9", 26_500_000, 28_500_000),
    "MHC_25-34Mb_GRCh37": ("6", 25_000_000, 34_000_000),
}
GENOME_WIDE = 5e-8
CHI2_MEDIAN_NULL = 0.4549  # median of chi-square with 1 df
_COLUMNS = (
    "rsid",
    "chromosome",
    "base_pair_location",
    "effect_allele",
    "other_allele",
    "effect_allele_frequency",
    "beta",
    "standard_error",
    "p_value",
    "N_effective",
)


def _variant(row, ix):
    return {
        "rsid": row[ix["rsid"]],
        "chromosome": row[ix["chromosome"]],
        "position_grch37": int(row[ix["base_pair_location"]]),
        "effect_allele": row[ix["effect_allele"]],
        "other_allele": row[ix["other_allele"]],
        "effect_allele_frequency": float(row[ix["effect_allele_frequency"]]),
        "beta": float(row[ix["beta"]]),
        "standard_error": float(row[ix["standard_error"]]),
        "p_value": float(row[ix["p_value"]]),
        "n_effective": float(row[ix["N_effective"]]),
    }


def run(path, regions=None):
    """Stream a GWAS Catalog summary-statistics file as submitted (stdlib only).

    GCST90027164 declares `is_harmonised: false` and GRCh37 in its own metadata; the catalogue's
    `harmonised/` deposit is a different file and is not what this analysis reads.
    """
    regions = REGIONS if regions is None else regions
    rows = genome_wide_hits = 0
    z2 = array("d")
    stats = {name: {"variants": 0, "genome_wide_significant": 0, "lead": None} for name in regions}
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        header = handle.readline().rstrip("\n").split("\t")
        if tuple(header) != _COLUMNS:
            raise ValueError(f"gwas schema drift: {header!r}")
        ix = {name: i for i, name in enumerate(header)}
        for line in handle:
            row = line.rstrip("\n").split("\t")
            p = float(row[ix["p_value"]])
            rows += 1
            genome_wide_hits += p < GENOME_WIDE
            se = float(row[ix["standard_error"]])
            if se > 0:
                z2.append((float(row[ix["beta"]]) / se) ** 2)
            for name, (chrom, lo, hi) in regions.items():
                if row[ix["chromosome"]] != chrom or not lo <= int(row[ix["base_pair_location"]]) <= hi:
                    continue
                entry = stats[name]
                entry["variants"] += 1
                entry["genome_wide_significant"] += p < GENOME_WIDE
                if entry["lead"] is None or p < entry["lead"]["p_value"]:
                    entry["lead"] = _variant(row, ix)
    if rows == 0:
        raise ValueError("gwas file has no rows")
    lambda_gc = statistics.median(z2) / CHI2_MEDIAN_NULL
    region_out = [
        {
            "region": name,
            "chromosome": regions[name][0],
            "start": regions[name][1],
            "end": regions[name][2],
            **stats[name],
        }
        for name in regions
    ]
    return {
        "analysis_id": "a3_gwas_loci",
        "method": (
            "Deterministic single pass over GWAS summary statistics (stdlib only): genome-wide counts at "
            "p<5e-8, lambda_GC = median(z^2)/0.4549, and the lowest-p variant per fixed window. "
            "Descriptive only: no LD, no conditioning, no fine-mapping, no colocalisation."
        ),
        "study": {
            "gwas_catalog_id": "GCST90027164",
            "genome_assembly": "GRCh37",
            "ancestry": "European (per the study metadata)",
        },
        "n_variants": rows,
        "n_genome_wide_significant": genome_wide_hits,
        "lambda_gc": round(lambda_gc, 4),
        "regions": region_out,
        "negative_result": False,
        "conclusion": (
            "Descriptive summary of the ALS GWAS signal in two windows chosen by us. It does not identify "
            "a causal variant or gene and says nothing about the autoimmune mechanism."
        ),
        "limitations": [
            "Windows are arbitrary; the lead variant of a window is not a fine-mapped signal.",
            "lambda_GC above 1 can reflect polygenicity as well as residual confounding.",
            "Summary statistics are GRCh37 and were not re-lifted or re-harmonised here.",
            "Nothing here supports a treatment, HLA causality or individual-level conclusion.",
        ],
    }
