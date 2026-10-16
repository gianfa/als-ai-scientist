"""Hard spend cap for a run: stop the launcher when the estimated cost of the whole run reaches the cap.

Why: older Claude Code CLI versions applied `claude --max-budget-usd` to the launcher session only, so a run's
agents could spend well past the cap. On CLI 2.1.287 that cap does stop the workflow's agents, so
`pipeline/launcher.py` hands each session what is left of the run's cap and the CLI enforces the same ceiling from
its own figures. This watcher stays as the independent backstop: it estimates
the cost of every agent, subagents included, from the same streams and transcripts the token observer reads
(`observability.wf_observer.tokens`), over every stage stream of the run directory (`*-output*.jsonl`: every
phase, retries and resumes). So the cap is on the run as a whole: a resume
counts what was already spent, and must be launched with a higher cap to go on.

Started by `pipeline/launcher.py` next to each phase launch; polls every `--interval` seconds; exits
by itself when the launcher process is gone. On reaching the cap it writes `logs/cost-cap-hit.json`, sends
SIGTERM to the launcher (SIGKILL after `--grace` seconds) and exits 3. The launcher's own `timeout` forwards the
signal to `claude`, and the interrupted run can be resumed (re-run with the same `--run-id`).

Limits: the cost is an estimate from `configs/tokens.yaml` prices; tokens of an unpriced model count as zero and
are reported in the log; spend between two polls is not seen until the next one.

Run (from the repository root): `uv run python pipeline/cost_watch.py RUN_DIR --pid PID --cap-usd 13`
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT))  # run as a script from pipeline/: make the first-party packages importable

from observability.wf_observer.tokens import aggregate, collect_usage, load_tokens_config  # noqa: E402

STREAM_GLOB = "*-output*.jsonl"
CAP_MARKER = "cost-cap-hit.json"
EXIT_CAP_HIT = 3


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def current_cost(run_dir: Path, config: dict[str, Any]) -> dict[str, Any]:
    """Estimated USD of the whole run so far, and the tokens no price covers."""
    streams = sorted(run_dir.glob(STREAM_GLOB))
    records, _ = collect_usage(streams)
    data = aggregate(records, config.get("limits", {}), config.get("prices_usd_per_mtok", {}))
    return {
        "est_usd": float(data["totals"]["est_usd"]),
        "unpriced_tokens": int(data.get("unpriced_tokens", 0)),
        "unpriced_models": list(data.get("unpriced_models", [])),
        "streams": [s.name for s in streams],
    }


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _append(log_path: Path, entry: dict[str, Any]) -> None:
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")


def watch(
    cost: Callable[[], dict[str, Any]],
    pid: int,
    cap_usd: float,
    log_path: Path,
    marker_path: Path,
    *,
    interval: float = 15.0,
    grace: float = 20.0,
    sleep: Callable[[float], None] = time.sleep,
    alive: Callable[[int], bool] = _alive,
    kill: Callable[[int, int], None] = os.kill,
    clock: Callable[[], str] = _utc_now,
) -> int:
    """Poll until the launcher exits (return 0) or the cap is reached (stop it, return EXIT_CAP_HIT).

    A failure to estimate is logged and retried at the next poll; it never stops the run by itself.
    """
    while alive(pid):
        try:
            state = cost()
        except Exception as err:  # noqa: BLE001 - an estimate that failed must not kill a paid run
            _append(log_path, {"ts": clock(), "event": "estimate_failed", "error": f"{type(err).__name__}: {err}"})
            sleep(interval)
            continue
        _append(log_path, {"ts": clock(), "event": "poll", "cap_usd": cap_usd, **state})
        if state["est_usd"] >= cap_usd:
            hit = {"ts": clock(), "event": "cap_hit", "cap_usd": cap_usd, "pid": pid, **state}
            marker_path.write_text(json.dumps(hit, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            _append(log_path, hit)
            kill(pid, signal.SIGTERM)
            waited = 0.0
            while alive(pid) and waited < grace:
                sleep(1.0)
                waited += 1.0
            if alive(pid):
                kill(pid, signal.SIGKILL)
                _append(log_path, {"ts": clock(), "event": "sigkill", "pid": pid})
            return EXIT_CAP_HIT
        sleep(interval)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("run_dir", type=Path, help="run results directory (experiments/<RUN_ID>/results)")
    parser.add_argument("--pid", type=int, required=True, help="launcher process to stop at the cap")
    parser.add_argument("--cap-usd", type=float, required=True, help="cap on the estimated cost of the whole run")
    parser.add_argument("--interval", type=float, default=15.0, help="seconds between polls (default 15)")
    parser.add_argument("--grace", type=float, default=20.0, help="seconds between SIGTERM and SIGKILL (default 20)")
    args = parser.parse_args(argv)
    if args.cap_usd <= 0:
        print("cost_watch: --cap-usd must be positive", file=sys.stderr)
        return 2
    run_dir = args.run_dir.resolve()
    (run_dir / "logs").mkdir(parents=True, exist_ok=True)
    config = load_tokens_config()
    return watch(
        lambda: current_cost(run_dir, config),
        args.pid,
        args.cap_usd,
        run_dir / "logs" / "cost-watch.jsonl",
        run_dir / "logs" / CAP_MARKER,
        interval=args.interval,
        grace=args.grace,
    )


if __name__ == "__main__":
    raise SystemExit(main())
