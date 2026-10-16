import hashlib
import json
from pathlib import Path


def digest(summary):
    payload = {key: value for key, value in summary.items() if key != "result_digest"}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def verify_summary(path):
    summary = json.loads(Path(path).read_text(encoding="utf-8"))
    expected = summary.get("result_digest")
    actual = digest(summary)
    if not expected or expected != actual:
        raise ValueError(f"result digest mismatch: expected {expected}, computed {actual}")
    return True


def verify_run(path, envelope_path=None):
    """Verify one run and optionally persist a compact, digest-bound receipt."""
    summary_path = Path(path)
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    verify_summary(summary_path)
    inputs = summary.get("inputs")
    artifacts = summary.get("artifacts")
    if not isinstance(inputs, list) or not inputs:
        raise ValueError("analysis summary has no inputs to verify")
    if not isinstance(artifacts, list) or not artifacts:
        raise ValueError("analysis summary has no artifacts to verify")
    input_checks = []
    inputs_verified = True
    for item in inputs:
        input_path = Path(item["path"])
        if not input_path.is_absolute():
            input_path = Path(__file__).resolve().parents[1] / input_path
        exists = input_path.is_file()
        matches = exists and hashlib.sha256(input_path.read_bytes()).hexdigest() == item.get("sha256")
        inputs_verified = inputs_verified and matches
        input_checks.append({"path": item["path"], "sha256": item.get("sha256"), "verified": bool(matches)})

    artifacts_verified = True
    artifact_checks = []
    for item in artifacts:
        artifact_path = summary_path.parent / item["path"]
        exists = artifact_path.is_file()
        actual_hash = hashlib.sha256(artifact_path.read_bytes()).hexdigest() if exists else None
        matches = exists and (item.get("sha256") is None or item["sha256"] == actual_hash)
        artifacts_verified = artifacts_verified and matches
        artifact_checks.append({"path": str(item["path"]), "sha256": actual_hash, "verified": bool(matches)})

    receipt = {
        "verification_version": "1",
        "analysis_id": summary.get("analysis_id"),
        "result_digest": summary.get("result_digest"),
        "summary_path": str(summary_path),
        "verified": bool(inputs_verified and artifacts_verified),
        "checks": {"result_digest": True, "inputs": bool(inputs_verified), "artifacts": bool(artifacts_verified)},
        "details": {"inputs": input_checks, "artifacts": artifact_checks},
    }
    payload = json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()
    receipt["verification_digest"] = hashlib.sha256(payload).hexdigest()
    if not inputs_verified or not artifacts_verified:
        raise ValueError("input or artifact verification failed")
    if envelope_path is not None:
        destination = Path(envelope_path)
        if destination.exists():
            raise FileExistsError(f"refusing to overwrite verification receipt: {destination}")
        destination.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return receipt


def verify_receipt(path):
    receipt_path = Path(path)
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    expected = receipt.get("verification_digest")
    payload = {key: value for key, value in receipt.items() if key != "verification_digest"}
    actual = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if not expected or expected != actual:
        raise ValueError(f"verification receipt digest mismatch: expected {expected}, computed {actual}")
    summary_path = Path(receipt["summary_path"])
    if not summary_path.is_absolute():
        summary_path = Path(__file__).resolve().parents[1] / summary_path
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    verify_summary(summary_path)
    if receipt.get("analysis_id") != summary.get("analysis_id") or receipt.get("result_digest") != summary.get(
        "result_digest"
    ):
        raise ValueError("verification receipt does not match its summary")
    if receipt.get("verified") is not True or any(
        receipt.get("checks", {}).get(key) is not True for key in ("result_digest", "inputs", "artifacts")
    ):
        raise ValueError("verification receipt contains a failed check")
    base = Path(__file__).resolve().parents[1]
    details = receipt.get("details", {})
    for kind in ("inputs", "artifacts"):
        checks = details.get(kind)
        if not isinstance(checks, list) or not checks or any(check.get("verified") is not True for check in checks):
            raise ValueError(f"verification receipt has invalid {kind} details")
        descriptors = summary.get(kind)
        if not isinstance(descriptors, list) or len(descriptors) != len(checks):
            raise ValueError(f"verification receipt {kind} do not match summary descriptors")
        for descriptor, check in zip(descriptors, checks):
            if not isinstance(descriptor, dict) or not isinstance(check, dict):
                raise ValueError(f"verification receipt has invalid {kind} details")
            if str(descriptor.get("path")) != check.get("path"):
                raise ValueError(f"verification receipt {kind} paths do not match summary")
            if kind == "inputs" and descriptor.get("sha256") != check.get("sha256"):
                raise ValueError("verification receipt input hashes do not match summary")
            if descriptor.get("sha256") is not None and descriptor["sha256"] != check.get("sha256"):
                raise ValueError(f"verification receipt {kind} hashes do not match summary")
            file_path = Path(check["path"])
            if kind == "inputs" and not file_path.is_absolute():
                file_path = base / file_path
            elif kind == "artifacts":
                file_path = summary_path.parent / file_path
            if not file_path.is_file():
                raise ValueError(f"verification receipt {kind} file is missing: {check['path']}")
            actual_hash = hashlib.sha256(file_path.read_bytes()).hexdigest()
            if not check.get("sha256") or actual_hash != check["sha256"]:
                raise ValueError(f"verification receipt {kind} file hash mismatch: {check['path']}")
    return True
