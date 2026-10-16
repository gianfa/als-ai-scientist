"""Print the effective technical requirements/capacity of the machine running the workflow, as JSON.

Standalone, stdlib-only, best-effort. Not wired into the CLI or the observer server/UI; `run.sh`
calls this module directly (not via a CLI subcommand) right after creating the experiment directory,
writing its stdout to `experiments/<RUN_ID>/results/sysinfo.json`.

Run from the repository root:

    python -m observability.wf_observer.sysinfo

"Effective" matters because this project runs inside containers: the host's total CPU/RAM is not
necessarily what the process can actually use. Where possible this reports both:
- the host-level figure (what the kernel/hardware reports), and
- the cgroup-limited figure (what a `docker run --cpus=... --memory=...`-style limit allows), when a
  cgroup limit is present and finite.

GPU detection is via `nvidia-smi` only (no torch/pynvml dependency): absent or inconclusive means
`"gpus": []`, not "no GPU" — this machine may still have one that `nvidia-smi` cannot reach (e.g. no
driver passthrough into the container).

Every probe is independently best-effort: a failure on one (e.g. `/proc` absent on a non-Linux host)
is recorded as `"error"` on that section rather than crashing the whole script, so the rest of the
report still comes through.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

CGROUP_ROOT = Path("/sys/fs/cgroup")


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def _cgroup_version() -> int | None:
    if (CGROUP_ROOT / "cgroup.controllers").exists():
        return 2
    if (CGROUP_ROOT / "memory" / "memory.limit_in_bytes").exists():
        return 1
    return None


def detect_container() -> dict[str, Any]:
    """Best-effort container detection. False negatives are possible (e.g. some unprivileged runtimes)."""
    signals: list[str] = []
    if Path("/.dockerenv").exists():
        signals.append("/.dockerenv")
    if Path("/run/.containerenv").exists():
        signals.append("/run/.containerenv (podman)")
    cgroup_content = _read_text(Path("/proc/1/cgroup")) or ""
    if any(marker in cgroup_content for marker in ("docker", "kubepods", "containerd", "lxc")):
        signals.append("/proc/1/cgroup")
    sched_content = _read_text(Path("/proc/1/sched")) or ""
    if sched_content and not sched_content.startswith("init "):
        # PID 1 is not the real init process — a strong container signal, but not by itself decisive.
        signals.append("/proc/1/sched (pid 1 is not init)")
    return {"likely_containerized": bool(signals), "signals": signals}


def cpu_info() -> dict[str, Any]:
    info: dict[str, Any] = {}
    try:
        info["logical_cpus_host"] = os.cpu_count()
    except Exception as err:  # pragma: no cover - defensive
        info["logical_cpus_host_error"] = str(err)

    try:
        info["logical_cpus_process_affinity"] = len(os.sched_getaffinity(0))
    except AttributeError:
        info["logical_cpus_process_affinity"] = None  # not available (e.g. macOS, Windows)
    except Exception as err:  # pragma: no cover - defensive
        info["logical_cpus_process_affinity_error"] = str(err)

    version = _cgroup_version()
    info["cgroup_version"] = version
    effective_quota_cpus: float | None = None
    try:
        if version == 2:
            raw = _read_text(CGROUP_ROOT / "cpu.max")
            if raw:
                quota_str, period_str = raw.split()
                if quota_str != "max":
                    effective_quota_cpus = int(quota_str) / int(period_str)
        elif version == 1:
            quota = _read_text(CGROUP_ROOT / "cpu" / "cpu.cfs_quota_us")
            period = _read_text(CGROUP_ROOT / "cpu" / "cpu.cfs_period_us")
            if quota and period and int(quota) > 0:
                effective_quota_cpus = int(quota) / int(period)
    except (OSError, ValueError) as err:
        info["cgroup_cpu_quota_error"] = str(err)
    info["cgroup_cpu_quota_cpus"] = effective_quota_cpus

    candidates = [
        v
        for v in (
            info.get("logical_cpus_process_affinity"),
            effective_quota_cpus,
            info.get("logical_cpus_host"),
        )
        if v
    ]
    info["effective_cpus"] = min(candidates) if candidates else None
    return info


_UNLIMITED_MEMORY_THRESHOLD = 1 << 62  # cgroup v1 reports a huge sentinel (not exactly 2**63-1) when unset


def memory_info() -> dict[str, Any]:
    info: dict[str, Any] = {}
    meminfo = _read_text(Path("/proc/meminfo"))
    host_total_bytes: int | None = None
    if meminfo:
        for line in meminfo.splitlines():
            if line.startswith("MemTotal:"):
                # e.g. "MemTotal:       16384000 kB"
                parts = line.split()
                if len(parts) >= 2 and parts[1].isdigit():
                    host_total_bytes = int(parts[1]) * 1024
                break
    else:
        info["proc_meminfo_error"] = "not available (non-Linux host?)"
    info["host_total_bytes"] = host_total_bytes

    version = _cgroup_version()
    info["cgroup_version"] = version
    limit_bytes: int | None = None
    try:
        if version == 2:
            raw = _read_text(CGROUP_ROOT / "memory.max")
            if raw and raw != "max":
                limit_bytes = int(raw)
        elif version == 1:
            raw = _read_text(CGROUP_ROOT / "memory" / "memory.limit_in_bytes")
            if raw and int(raw) < _UNLIMITED_MEMORY_THRESHOLD:
                limit_bytes = int(raw)
    except (OSError, ValueError) as err:
        info["cgroup_memory_limit_error"] = str(err)
    info["cgroup_limit_bytes"] = limit_bytes

    candidates = [v for v in (limit_bytes, host_total_bytes) if v]
    info["effective_bytes"] = min(candidates) if candidates else None
    if info["effective_bytes"] is not None:
        info["effective_gib"] = round(info["effective_bytes"] / (1024**3), 2)
    return info


def process_limits() -> dict[str, Any]:
    """Limits on the number of processes/threads (one pid per thread on Linux).

    A container started with `--pids-limit` makes thread creation fail once exceeded, which a
    runtime may turn into an abort. `pids_current` is a point-in-time value, not a peak.
    """
    info: dict[str, Any] = {}
    version = _cgroup_version()
    info["cgroup_version"] = version
    if version == 2:
        for key, name in (("pids_max", "pids.max"), ("pids_current", "pids.current")):
            raw = _read_text(CGROUP_ROOT / name)
            info[key] = None if raw is None else (raw if raw == "max" else int(raw))
    try:
        import resource

        soft, hard = resource.getrlimit(resource.RLIMIT_NPROC)
        info["rlimit_nproc_soft"] = None if soft == resource.RLIM_INFINITY else soft
        info["rlimit_nproc_hard"] = None if hard == resource.RLIM_INFINITY else hard
    except (ImportError, OSError, ValueError) as err:
        info["rlimit_nproc_error"] = str(err)
    raw = _read_text(Path("/proc/sys/kernel/threads-max"))
    info["kernel_threads_max"] = int(raw) if raw and raw.isdigit() else None
    return info


def gpu_info() -> dict[str, Any]:
    nvidia_smi = shutil.which("nvidia-smi")
    if not nvidia_smi:
        return {"available": False, "gpus": [], "method": None}
    try:
        out = subprocess.run(
            [nvidia_smi, "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as err:
        return {"available": False, "gpus": [], "method": "nvidia-smi", "error": str(err)}
    gpus = []
    for line in out.stdout.strip().splitlines():
        fields = [f.strip() for f in line.split(",")]
        if len(fields) == 3:
            gpus.append({"name": fields[0], "memory_total": fields[1], "driver_version": fields[2]})
    return {"available": bool(gpus), "gpus": gpus, "method": "nvidia-smi"}


def os_info() -> dict[str, Any]:
    info = {
        "system": platform.system(),
        "release": platform.release(),
        "version": platform.version(),
        "machine": platform.machine(),
        "python_version": platform.python_version(),
    }
    try:
        info["hostname"] = socket.gethostname()
    except OSError as err:  # pragma: no cover - defensive
        info["hostname_error"] = str(err)
    return info


def collect() -> dict[str, Any]:
    return {
        "os": os_info(),
        "container": detect_container(),
        "cpu": cpu_info(),
        "memory": memory_info(),
        "process_limits": process_limits(),
        "gpu": gpu_info(),
    }


def main() -> int:
    print(json.dumps(collect(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
