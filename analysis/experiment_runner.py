"""Run a registered experiment handler on manifest-bound inputs.

This is the narrow Python boundary that the agent tool calls. The caller must separately prove that an EXP was
bought; this module never accepts code, file paths or arbitrary parameters from an agent. A handler is registered
here, with the exact sources it reads and the preregistered meaning of each of its outcomes, or it does not run.
"""

from __future__ import annotations

import json
import platform
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tools.data.local_sources import by_id, path_of, sha256_path

from . import ANALYSIS_VERSION
from . import experiment_hla_panel as hla
from . import experiment_ics_cytokines as ics
from .datasets import sha256
from .runner import _git_commit, _write_result
from .verify import verify_receipt, verify_run


@dataclass(frozen=True)
class Handler:
    analysis_id: str
    input_ids: tuple[str, ...]  # manifest source IDs, in the order `run` takes their paths
    script_path: str
    run: Callable[..., dict[str, Any]]
    files: Callable[..., list[Path]]  # the files `run` actually reads, hashed into the summary
    interpretations: dict[str, str]  # outcome -> the only interpretation an agent may give it
    readout: Callable[[dict[str, Any]], dict[str, Any]]


def _ics_files(cytometry: Path, source_data: Path) -> list[Path]:
    return [*(w for record in ics.ICS_RECORDS for w in sorted((cytometry / record).glob("*.wsp"))), source_data]


HANDLERS: dict[str, Handler] = {
    h.analysis_id: h
    for h in (
        Handler(
            analysis_id=hla.ANALYSIS_ID,
            input_ids=("michaelis-table1-epitopes", "michaelis-table2-hla", "iedb-netmhciipan-cache"),
            script_path="analysis/experiment_hla_panel.py",
            run=hla.run,
            files=lambda *paths: list(paths),
            interpretations={
                "panel_condition_met": "supports_panel_condition_only",
                "panel_condition_failed": "weakens_panel_condition_only",
            },
            readout=lambda s: {
                "selected_pool_size": len(s["selected_peptides"]),
                "covered_alleles": s["covered_alleles"],
                "alleles_in_panel": len(s["alleles"]),
                "control_full_coverage_fraction": s["control_full_coverage_fraction"],
            },
        ),
        Handler(
            analysis_id=ics.ANALYSIS_ID,
            input_ids=("michaelis-zenodo-cytometry", "michaelis-source-data"),
            script_path="analysis/experiment_ics_cytokines.py",
            run=ics.run,
            files=_ics_files,
            interpretations={
                "skew_replicated": "supports_ifng_skew_only",
                "skew_not_replicated": "weakens_ifng_skew_only",
            },
            readout=lambda s: {
                "p_value": s["p_value"],
                "median_skew_ratio": s["median_skew_ratio"],
                "groups": s["groups"],
                "donors_reproducing_source_data": s["reproduction"]["reproduced"],
                "donors": s["reproduction"]["donors"],
            },
        ),
    )
}


def handler(analysis_id: str) -> Handler:
    if analysis_id not in HANDLERS:
        raise ValueError(f"no registered handler for analysis {analysis_id!r}")
    return HANDLERS[analysis_id]


def execute(
    manifest: dict[str, Any],
    root: Path,
    output: Path,
    run_id: str,
    experiment_id: str,
    config_hash: str,
    analysis_id: str,
) -> dict[str, Any]:
    """Execute once or verify and reuse an existing artifact. A failed input check writes nothing."""
    registered = handler(analysis_id)
    entries = by_id(manifest)
    paths = []
    for source_id in registered.input_ids:
        entry = entries.get(source_id)
        if entry is None:
            raise ValueError(f"manifest lacks required source {source_id}")
        path = path_of(manifest, source_id, root)
        if sha256_path(path) != entry.get("sha256"):
            raise ValueError(f"manifest hash mismatch for {source_id}")
        paths.append(path)
    refs = {source_id: entries[source_id]["ref"] for source_id in registered.input_ids}
    inputs = []
    for file in registered.files(*paths):
        source_id = next(sid for sid, p in zip(registered.input_ids, paths) if file == p or p in file.parents)
        inputs.append({"id": source_id, "path": str(file.resolve()), "sha256": sha256(file), "ref": refs[source_id]})

    summary_path = output / "summary.json"
    receipt_path = output / "verification.json"
    if output.exists():
        if not summary_path.is_file() or not receipt_path.is_file():
            raise ValueError(f"incomplete existing experiment artifact: {output}")
        verify_receipt(receipt_path)
        summary = json.loads(summary_path.read_text())
        if (
            summary.get("experiment_id") != experiment_id
            or summary.get("analysis_id") != analysis_id
            or summary.get("provenance", {}).get("run_id") != run_id
            or summary.get("provenance", {}).get("config_hash") != config_hash
            or summary.get("inputs") != inputs
        ):
            raise ValueError("existing experiment artifact does not match this request")
        return summary

    result = registered.run(*paths)
    result.update(
        {
            "experiment_id": experiment_id,
            "analysis_version": ANALYSIS_VERSION,
            "script_path": registered.script_path,
            "git_commit": _git_commit(root),
            "inputs": inputs,
            "provenance": {"run_id": run_id, "config_hash": config_hash, "python_version": platform.python_version()},
            "artifacts": [{"path": "summary.json", "kind": "json"}],
        }
    )
    _write_result(result, output)
    verify_run(summary_path, envelope_path=receipt_path)
    verify_receipt(receipt_path)
    return result
