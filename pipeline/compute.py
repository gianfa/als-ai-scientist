"""The `compute` phase: deterministic analyses, and the claims they license.

No agent runs here. Each analysis is the existing code in `analysis/`, invoked on the paths the `data`
phase declared and hashed, writing its own verifiable artifact (`summary.json` with a `result_digest`, checked by
`analysis/verify.py`). What this module adds is turning those artifacts into knowledge-
base claims, so a computed number can be cited by a later agent exactly like a quoted sentence.

Two rules shape the mapping, both about not saying more than the artifact says:

- A claim's `content` is templated from the artifact's own `results` entries (`key`, `value`, `n`, `test`). It is
  terse on purpose: interpreting a number is the work of an agent that cites the claim, not of this code.
- The analysis' own `conclusion` becomes one claim, because it is written in the analysis script, is deterministic,
  and is where a negative result is stated. `negative_result: true` is carried in the content, not dropped.

Every claim is `origin: computation`, `verification_method: code`, `provenance_status: verified`: it is verified in
the sense that re-running the code on the same inputs reproduces it, and its `source_refs` name both the dataset
versions (`dataset:<id>@<hash>`) and the artifact (`result:<analysis_id>@<digest>`) that would change if it did not.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from analysis.runner import run_analysis, run_gwas_analysis, run_hla_analysis
from analysis.verify import verify_run, verify_summary
from tools.data.local_sources import HASH_PREFIX, MissingSource, path_of

# Which declared sources each analysis consumes. A3's single input is the GWAS file; A1 and A2 read both tables
# (A2 uses Table 1's start positions, and Table 2 is parsed for its schema check and recorded as provenance).
ANALYSIS_SOURCES: dict[str, tuple[str, ...]] = {
    "a1_immunodominance": ("michaelis-table1-epitopes", "michaelis-table2-hla"),
    "a2_epitope_clustering": ("michaelis-table1-epitopes", "michaelis-table2-hla"),
    "a3_gwas_loci": ("als-gwas-gcst90027164",),
    # A4 reads the two tables and the prediction cache acquired once over the IEDB API. The cache is
    # an input like any other: hashed by the `data` phase, and the analysis runs with the network forbidden.
    "a4_hla_binding": ("michaelis-table1-epitopes", "michaelis-table2-hla", "iedb-netmhciipan-cache"),
}


class AnalysisFailed(RuntimeError):
    """An analysis could not run or its artifact did not verify. It is never swallowed: a computed claim that
    rests on an unverified artifact would be a provenance violation."""


@dataclass(frozen=True)
class ComputedClaim:
    """One claim a verified artifact licenses, with the references that make it checkable."""

    content: str
    type: str  # quantitative | observational
    source_refs: list[str]


# Keys of the artifact that say which run wrote it rather than what was computed. The artifact's own
# `result_digest` covers them, so it differs between two runs of the same code on the same inputs (two runs
# produce identical claims under different digests). A claim's reference must
# name the computation, not the run, or the same fact would carry a different provenance every time.
_RUN_SPECIFIC = ("provenance", "result_digest", "git_commit")


def computation_ref(analysis_id: str, summary: dict[str, Any]) -> str:
    """`result:<analysis_id>@<digest>`, the digest taken over everything but the run-specific keys.

    Reproducing the analysis from the same inputs with the same script and seed yields the same reference, so a
    claim seen again across runs keeps a reference that is true for both. The artifact's own `result_digest`
    remains what `analysis/verify.py` checks; this is a citation, not a replacement for it.
    """
    payload = {k: v for k, v in summary.items() if k not in _RUN_SPECIFIC}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return f"result:{analysis_id}@{digest[:HASH_PREFIX]}"


def claims_of(analysis_id: str, summary: dict[str, Any], dataset_refs: list[str]) -> list[ComputedClaim]:
    """The claims of one verified artifact: one per numeric result, plus its conclusion."""
    refs = [*dataset_refs, computation_ref(analysis_id, summary)]
    claims = []
    for entry in summary.get("results", []):
        parts = [f"{analysis_id}: {entry['key']} = {entry['value']}"]
        if entry.get("n") is not None:
            parts.append(f"n = {entry['n']}")
        if entry.get("test"):
            parts.append(str(entry["test"]))
        claims.append(ComputedClaim(content="; ".join(parts) + ".", type="quantitative", source_refs=list(refs)))
    conclusion = str(summary.get("conclusion") or "").strip()
    if conclusion:
        prefix = "Negative result" if summary.get("negative_result") else "Result"
        claims.append(
            ComputedClaim(
                content=f"{prefix} of {analysis_id}: {conclusion}", type="observational", source_refs=list(refs)
            )
        )
    return claims


def _load_verified(summary_path: Path) -> dict[str, Any]:
    """An artifact from an earlier attempt is reused only if its own digest still checks out."""
    summary = json.loads(summary_path.read_text())
    verify_summary(summary_path)
    return summary


def run_one(
    analysis_id: str, manifest: dict[str, Any], root: Path, output_root: Path, seed: int, run_id: str, config_hash: str
) -> tuple[dict[str, Any], list[ComputedClaim]]:
    """Run (or reuse) one analysis and return its summary with the claims it licenses.

    The analysis writes into `output_root/<analysis_id>/`, which the runner refuses to overwrite. A re-attempt of
    the phase therefore reuses the existing artifact after verifying it, instead of silently recomputing over it.
    """
    if analysis_id not in ANALYSIS_SOURCES:
        raise AnalysisFailed(f"unknown analysis {analysis_id!r}: known are {sorted(ANALYSIS_SOURCES)}")
    output = output_root / analysis_id
    summary_path = output / "summary.json"
    try:
        paths = {sid: path_of(manifest, sid, root) for sid in ANALYSIS_SOURCES[analysis_id]}
    except MissingSource as err:
        raise AnalysisFailed(f"{analysis_id}: {err}") from err
    try:
        if summary_path.is_file():
            summary = _load_verified(summary_path)
        elif analysis_id == "a4_hla_binding":
            summary = run_hla_analysis(
                paths["michaelis-table1-epitopes"],
                paths["michaelis-table2-hla"],
                paths["iedb-netmhciipan-cache"],
                output,
                seed=seed,
                run_id=run_id,
                config_hash=config_hash,
            )
            verify_run(summary_path, envelope_path=output / "verification.json")
        elif analysis_id == "a3_gwas_loci":
            summary = run_gwas_analysis(
                paths["als-gwas-gcst90027164"], output, seed=seed, run_id=run_id, config_hash=config_hash
            )
            verify_run(summary_path, envelope_path=output / "verification.json")
        else:
            summary = run_analysis(
                analysis_id,
                None,
                output,
                seed=seed,
                run_id=run_id,
                config_hash=config_hash,
                table1=paths["michaelis-table1-epitopes"],
                table2=paths["michaelis-table2-hla"],
            )
            verify_run(summary_path, envelope_path=output / "verification.json")
    except Exception as err:  # noqa: BLE001 - re-raised with the analysis named
        raise AnalysisFailed(f"{analysis_id}: {type(err).__name__}: {err}") from err
    refs = [entry["ref"] for entry in manifest["sources"] if entry["id"] in ANALYSIS_SOURCES[analysis_id]]
    return summary, claims_of(analysis_id, summary, refs)


def run_all(
    analyses: list[str],
    manifest: dict[str, Any],
    root: Path,
    output_root: Path,
    seed: int,
    run_id: str,
    config_hash: str,
    runner: Callable[..., tuple[dict[str, Any], list[ComputedClaim]]] = run_one,
) -> tuple[list[ComputedClaim], dict[str, Any]]:
    """Every configured analysis, in order, with a per-analysis report. The first failure stops the phase."""
    claims: list[ComputedClaim] = []
    report: dict[str, Any] = {}
    for analysis_id in analyses:
        summary, found = runner(analysis_id, manifest, root, output_root, seed, run_id, config_hash)
        claims += found
        report[analysis_id] = {
            "result_digest": summary["result_digest"],
            "negative_result": bool(summary.get("negative_result")),
            "results": {e["key"]: e["value"] for e in summary.get("results", [])},
            "claims": len(found),
            "artifact": str((output_root / analysis_id / "summary.json").relative_to(output_root.parent)),
        }
    return claims, report


__all__ = ["ANALYSIS_SOURCES", "AnalysisFailed", "ComputedClaim", "claims_of", "computation_ref", "run_all", "run_one"]
