import itertools
import random


def run_count(occupied, grid_size):
    occupied = set(occupied)
    return sum(slot in occupied and slot - 1 not in occupied for slot in range(grid_size))


def exact_run_distribution(grid_size, occupied_count):
    if grid_size < 1 or occupied_count < 0 or occupied_count > grid_size:
        raise ValueError("invalid grid dimensions")
    distribution = {}
    for slots in itertools.combinations(range(grid_size), occupied_count):
        runs = run_count(slots, grid_size)
        distribution[runs] = distribution.get(runs, 0) + 1
    return distribution


def run(starts, grid_size=100, replicates=100000, seed=20260928):
    if not starts:
        raise ValueError("at least one occupied peptide slot is required")
    if any(start < 1 or (start - 1) % 5 for start in starts):
        raise ValueError("peptide starts must be 1-based and congruent to 1 modulo 5")
    occupied = sorted({(start - 1) // 5 for start in starts})
    if len(occupied) != len(starts):
        raise ValueError("duplicate occupied peptide slots are not allowed")
    if max(occupied) >= grid_size:
        raise ValueError("occupied slot exceeds grid")
    observed_runs = run_count(occupied, grid_size)
    rng = random.Random(seed)
    fewer_or_equal = 0
    slots = list(range(grid_size))
    for _ in range(replicates):
        sample = rng.sample(slots, len(occupied))
        if run_count(sample, grid_size) <= observed_runs:
            fewer_or_equal += 1
    return {
        "analysis_id": "a2_epitope_clustering",
        "grid_size": grid_size,
        "occupied_count": len(occupied),
        "occupied_slots": occupied,
        "observed_runs": observed_runs,
        "replicates": replicates,
        "seed": seed,
        "monte_carlo_count": fewer_or_equal,
        "p_value": max(1 / (replicates + 1), fewer_or_equal / (replicates + 1)),
        "method": "One-sided seeded Monte Carlo under uniform selection of occupied slots; statistic is maximal adjacent-run count.",
        "negative_result": fewer_or_equal / (replicates + 1) > 0.05,
        "conclusion": "The result describes regional occupancy, not independent epitope cores; adjacent 15-mers overlap by 10 residues.",
        "limitations": [
            "The 100-slot grid and step-5 tiling are derived from the extracted table and paper description.",
            "Unequal per-peptide donor denominators are not modeled.",
            "Adjacent peptides mechanically share sequence; clustering is not evidence of multiple independent cores.",
            "This analysis cannot establish mechanism, HLA causality, efficacy, or treatment advice.",
        ],
    }
