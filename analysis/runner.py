import hashlib
import json
import platform
import subprocess
from pathlib import Path

from . import ANALYSIS_VERSION
from .a1_immunodominance import run as run_a1
from .a2_epitope_clustering import run as run_a2
from .a3_gwas_loci import run as run_a3
from .a4_hla_binding import run as run_a4
from .datasets import load_table1, load_table2, sha256


def _git_commit(root):
    try:
        return subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def run_analysis(
    analysis_id, source_root, output_root, seed=20260928, run_id=None, config_hash=None, table1=None, table2=None
):
    """A1/A2 on the two extracted tables.

    `table1`/`table2` name the input files explicitly; this is how the pipeline passes the paths its `data` phase
    declared and hashed, so the run's provenance is the manifest's and not a path derived in here. Without them
    the paths are derived from `source_root`, which is what the CLI (`analysis/cli.py`) still does.
    """
    output_root = Path(output_root)
    if table1 is None or table2 is None:
        source_root = Path(source_root)
        table1 = source_root / "assets/table/table-p0006-001.csv"
        table2 = source_root / "assets/table/table-p0006-007.csv"
    table1, table2 = Path(table1), Path(table2)
    if analysis_id not in {"a1_immunodominance", "a2_epitope_clustering"}:
        raise ValueError(f"unsupported analysis: {analysis_id}")
    if not table1.is_file() or not table2.is_file():
        raise FileNotFoundError("paper2agent Table 1/Table 2 files are required")
    rows = load_table1(table1)
    load_table2(table2)  # schema check and provenance; A1 does not use HLA rows.
    if analysis_id == "a1_immunodominance":
        result = run_a1(rows, seed=seed)
        script_path = "analysis/a1_immunodominance.py"
    else:
        starts = [row.start for row in rows if not row.phospho]
        result = run_a2(starts, seed=seed)
        script_path = "analysis/a2_epitope_clustering.py"
    inputs = [
        {
            "path": str(path),
            "sha256": sha256(path),
            "license_note": "Source extraction from Michaelis et al. (2025), CC BY-NC-ND 4.0; local paper2agent output, not redistributed.",
        }
        for path in (table1, table2)
    ]
    result.update(
        {
            "analysis_version": ANALYSIS_VERSION,
            "script_path": script_path,
            "git_commit": _git_commit(Path(__file__).parents[1]),
            "inputs": inputs,
            "provenance": {
                "run_id": run_id or output_root.name,
                "config_hash": config_hash,
                "python_version": platform.python_version(),
            },
            "artifacts": [
                {"path": "summary.json", "kind": "json"},
                {"path": "table1_epitopes.csv", "kind": "csv", "sha256": sha256(table1)},
                {"path": "table2_hla_restrictions.csv", "kind": "csv", "sha256": sha256(table2)},
            ],
            "results": (
                [
                    {
                        "key": "primary_comparisons",
                        "value": len(result["pairwise_fisher"]),
                        "n": len(result["table1_rows"]),
                        "test": "Fisher exact + Holm",
                    },
                    {
                        "key": "rank_one_probability",
                        "value": result["rank_stability"]["rank_one_probability"],
                        "n": result["rank_stability"]["replicates"],
                        "test": "binomial bootstrap",
                    },
                ]
                if analysis_id == "a1_immunodominance"
                else [
                    {
                        "key": "observed_runs",
                        "value": result["observed_runs"],
                        "n": result["occupied_count"],
                        "test": "run count",
                    },
                    {
                        "key": "p_value",
                        "value": result["p_value"],
                        "n": result["replicates"],
                        "test": "seeded Monte Carlo",
                    },
                ]
            ),
            "supports_evidence_ids": [],
            "contradicts_evidence_ids": [],
        }
    )
    _write_result(result, output_root)
    for source, name in ((table1, "table1_epitopes.csv"), (table2, "table2_hla_restrictions.csv")):
        (output_root / name).write_bytes(source.read_bytes())
    return result


def _write_result(result, output_root):
    payload = {key: value for key, value in result.items() if key != "result_digest"}
    result["result_digest"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if output_root.exists():
        raise FileExistsError(f"refusing to overwrite existing output: {output_root}")
    output_root.mkdir(parents=True)
    (output_root / "summary.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def run_gwas_analysis(gwas_file, output_root, seed=20260928, run_id=None, config_hash=None):
    """A3 reads a raw GWAS file (never copied into the output) and is fully deterministic; `seed` is recorded only."""
    gwas_file = Path(gwas_file).resolve()
    output_root = Path(output_root)
    if not gwas_file.is_file():
        raise FileNotFoundError(f"GWAS summary statistics are required: {gwas_file}")
    result = run_a3(gwas_file)
    result.update(
        {
            "analysis_version": ANALYSIS_VERSION,
            "script_path": "analysis/a3_gwas_loci.py",
            "seed": seed,
            "git_commit": _git_commit(Path(__file__).parents[1]),
            "inputs": [
                {
                    "path": str(gwas_file),
                    "sha256": sha256(gwas_file),
                    "license_note": "GWAS Catalog GCST90027164 (van Rheenen et al. 2021); third-party, git-ignored, not redistributed. Check the GWAS Catalog terms before release.",
                }
            ],
            "provenance": {
                "run_id": run_id or output_root.name,
                "config_hash": config_hash,
                "python_version": platform.python_version(),
            },
            "artifacts": [{"path": "summary.json", "kind": "json"}],
            "results": [
                {
                    "key": "lambda_gc",
                    "value": result["lambda_gc"],
                    "n": result["n_variants"],
                    "test": "median chi-square",
                },
                {
                    "key": "n_genome_wide_significant",
                    "value": result["n_genome_wide_significant"],
                    "n": result["n_variants"],
                    "test": "p<5e-8",
                },
            ]
            + [
                {
                    "key": f"lead_p_value:{r['region']}",
                    "value": r["lead"]["p_value"],
                    "n": r["variants"],
                    "test": "minimum p in window",
                }
                for r in result["regions"]
                if r["lead"]
            ],
            "supports_evidence_ids": [],
            "contradicts_evidence_ids": [],
        }
    )
    _write_result(result, output_root)
    return result


def run_hla_analysis(table1, table2, cache, output_root, seed=20260928, run_id=None, config_hash=None):
    """A4 reads the two tables and the prediction cache, and never the network.

    The cache is hashed like any other input, so the artifact states which predictions it was computed over: a
    later acquisition appends to the file, changes its hash, and the digest no longer matches — which is the
    point, since the API exposes no model version.
    """
    table1, table2, cache = Path(table1).resolve(), Path(table2).resolve(), Path(cache).resolve()
    for path in (table1, table2, cache):
        if not path.is_file():
            raise FileNotFoundError(f"A4 input is required: {path}")
    result = run_a4(table1, table2, cache)
    result.update(
        {
            "analysis_version": ANALYSIS_VERSION,
            "script_path": "analysis/a4_hla_binding.py",
            "seed": seed,
            "git_commit": _git_commit(Path(__file__).parents[1]),
            "inputs": [
                {"path": str(table1), "sha256": sha256(table1)},
                {"path": str(table2), "sha256": sha256(table2)},
                {
                    "path": str(cache),
                    "sha256": sha256(cache),
                    "license_note": "IEDB Analysis Resource / NetMHCIIpan predictions, acquired once "
                    "under the DTU academic licence (LICENSES.md); "
                    "third-party output, git-ignored, not redistributed. The licence "
                    "forbids publishing benchmark results of the predictor.",
                },
            ],
            "provenance": {
                "run_id": run_id or output_root.name,
                "config_hash": config_hash,
                "python_version": platform.python_version(),
            },
            "artifacts": [{"path": "summary.json", "kind": "json"}],
            "supports_evidence_ids": [],
            "contradicts_evidence_ids": [],
        }
    )
    _write_result(result, Path(output_root))
    return result
