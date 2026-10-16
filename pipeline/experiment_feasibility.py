"""Deterministic data-feasibility check for proposed experiments.

This checks declared inputs against the run's manifest. It does not decide whether an
analysis exists or whether its design is biologically sound.
"""

from __future__ import annotations

from typing import Any

from analysis.experiment_runner import HANDLERS
from knowledge_base.schemas import Experiment
from tools.data.cytometry import canonical_marker, marker_family


def dataset_id(ref: str) -> str:
    return ref.removeprefix("dataset:").split("@", 1)[0].strip()


def assess(experiment: Experiment, manifest: dict[str, Any] | None) -> dict[str, Any]:
    """One inspectable verdict. Missing facts outrank unknown facts; neither means ready."""
    result: dict[str, Any] = {
        "experiment_id": experiment.id,
        "hypothesis_id": experiment.hypothesis_id,
        "dataset_refs": list(experiment.dataset_refs),
        "required_markers": list(experiment.required_markers),
        "required_metadata": list(experiment.required_metadata),
        "analysis_id": experiment.analysis_id,
        "sources": [],
        "matched": [],
        "missing": [],
        "unknown": [],
    }
    if experiment.kind != "in_silico":
        result.update(status="not_applicable", reason="wet-lab proposal; no local data execution claimed")
        return result

    sources = {e["id"]: e for e in (manifest or {}).get("sources", []) if isinstance(e, dict) and "id" in e}
    absent = {e["id"] for e in (manifest or {}).get("absent", []) if isinstance(e, dict) and "id" in e}
    selected = []
    for ref in experiment.dataset_refs:
        sid = dataset_id(ref)
        entry = sources.get(sid)
        if entry is None:
            if sid in absent or experiment.dataset_location == "remote":
                result["missing"].append(f"dataset:{sid}")
            else:
                result["unknown"].append(f"dataset:{sid} is not in this run's manifest")
            continue
        selected.append(entry)
        result["sources"].append({"id": sid, "ref": entry.get("ref"), "sha256": entry.get("sha256")})
        if ref.startswith("dataset:") and "@" in ref and ref != entry.get("ref"):
            result["missing"].append(f"dataset:{sid} version differs from the manifest")

    if experiment.analysis_id:
        input_ids = HANDLERS[experiment.analysis_id].input_ids
        if set(map(dataset_id, experiment.dataset_refs)) != set(input_ids):
            result["missing"].append(f"registered analysis requires exactly its {len(input_ids)} declared source IDs")
        elif len(selected) == len(input_ids):
            result["matched"].append(f"registered_inputs:{experiment.analysis_id}")
    elif not experiment.required_markers and not experiment.required_metadata:
        result["unknown"].append("required measurements were not declared")

    marker_sources = [e for e in selected if isinstance(e.get("shape", {}).get("markers"), list)]
    if experiment.required_markers:
        if len(marker_sources) != 1:
            result["unknown"].append("one marker inventory could not be identified")
        else:
            stored = marker_sources[0]["shape"]["markers"]
            # The inventory is written canonicalised (`markers_of`), so a raw string comparison would report
            # `IFN-gamma`, `IFN-γ` and `il-10` as absent from a panel that measures them: a false
            # `missing_inputs`, the opposite of the error this check exists to prevent.
            available = {canonical_marker(m) for m in stored}
            families = {marker_family(m) for m in stored}
            for marker in experiment.required_markers:
                item = f"marker:{marker}"
                if canonical_marker(marker) in available:
                    result["matched"].append(item)
                elif marker_family(marker) in families:
                    # `marker_family` collapses every interferon and every TNF to one key, so this is a
                    # resemblance, not a presence: it must not license `data_supported`, and it must not be
                    # reported as absent either. The designer is told what the panel actually records.
                    near = sorted(m for m in stored if marker_family(m) == marker_family(marker))
                    result["unknown"].append(
                        f"marker:{marker} resembles {', '.join(near)} in the recorded panel, which does not "
                        f"distinguish the subtype; declare the name the inventory uses"
                    )
                else:
                    result["missing"].append(item)

    # Mirrors `marker_sources` above, for the same reason: an in-silico EXP can (and `ics_cytokine_skew_v1`'s
    # always does) declare more than one `dataset_ref`, and only one of them is a source `read_panel` could
    # inventory. Building `inventories` from every selected source, as this once did, meant
    # `len(inventories) != 1` was true whenever an experiment named a second source at all, regardless of
    # whether a usable inventory existed — exactly the shape of an earlier run's EXP-1/5/7, each naming the
    # cytometry deposit alongside the source-data spreadsheet.
    metadata_sources = [e for e in selected if isinstance(e.get("shape", {}).get("metadata_fields"), list)]
    for field in experiment.required_metadata:
        if len(metadata_sources) != 1:
            result["unknown"].append(f"metadata:{field} has no inventory")
        else:
            stored = metadata_sources[0]["shape"]["metadata_fields"]
            result["matched" if field in stored else "missing"].append(f"metadata:{field}")

    if result["missing"]:
        status = "missing_inputs"
        reason = "; ".join(result["missing"])
    elif result["unknown"]:
        status = "unverified"
        reason = "; ".join(result["unknown"])
    else:
        status = "data_supported"
        reason = "declared measurements found in the recorded dataset inventory; analysis code is not implied"
    result.update(status=status, reason=reason)
    return result


__all__ = ["assess", "dataset_id"]
