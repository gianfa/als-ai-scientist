"""The pipeline's command line: the verbs of a run.

    uv run python -m pipeline.cli run  --id pl-001 [--kb knowledgebase] [--phases literature,generate] [--no-novelty]
    uv run python -m pipeline.cli stop --id pl-001
    uv run python -m pipeline.cli novelty --id pl-001 [--again]

Results go to `experiments/<run-id>/results/`; re-running with the same run ID resumes after the last completed
phase. A live run is paid: it launches one `claude -p` per phase.

When `synthesize` has completed, `run` goes on to the prior-art stage (`novelty`) under the same cost cap; `--no-novelty`
leaves it out, and a stage the cap cannot pay for is reported without failing the run. `novelty` alone runs it later.

`run.sh` is the launch entry point and stays exactly that — one job, one name.
The verbs live here, where the logic is. `--run-id` and `--id` are the same option, so every documented
command keeps working: a bare `--run-id …` with no verb is read as `run`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from tools.data.local_sources import MissingSource
from tools.literature.search import DEFAULT_CACHE as SEARCH_CACHE

from .config import DEFAULT_CONFIG, PACKAGE_ROOT, load_config
from .launcher import ClaudeLauncher, PhaseFailed
from .orchestrator import PHASES, Orchestrator
from .stop import GRACE_S, RUN_PID_FILE, clear_pid_file, completed_phases, stop_run, write_pid_file

# `PACKAGE_ROOT` is the repository root.
REPO_ROOT = PACKAGE_ROOT


VERBS = ("run", "stop", "novelty")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    launch = sub.add_parser("run", help="launch or resume a run (PAID: one `claude -p` session per phase)")
    launch.add_argument("--run-id", "--id", dest="run_id", required=True)
    launch.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    launch.add_argument("--kb", type=Path, default=REPO_ROOT / "knowledgebase")
    launch.add_argument("--phases", default=",".join(PHASES))
    launch.add_argument("--config-change", help="reason for resuming this run with a different config (recorded)")
    launch.add_argument(
        "--no-novelty",
        action="store_true",
        help="do not run the prior-art (novelty) stage after `synthesize`; `pipeline.cli novelty` runs it later",
    )

    novelty = sub.add_parser(
        "novelty",
        help="look for prior art for each hypothesis of a FINISHED run "
        "(PAID: one `claude -p` session; `run` launches it after `synthesize` unless given --no-novelty)",
    )
    novelty.add_argument("--run-id", "--id", dest="run_id", required=True)
    novelty.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    novelty.add_argument(
        "--again",
        action="store_true",
        help="a second attempt: the first attempt's files move to novelty-attempt-<n>/ "
        "in the archive, nothing is deleted",
    )

    halt = sub.add_parser("stop", help="stop a live run from the pids it recorded, never from the process table")
    halt.add_argument("--run-id", "--id", dest="run_id", required=True)
    halt.add_argument(
        "--grace-s", type=float, default=GRACE_S, help=f"seconds between SIGTERM and SIGKILL (default {GRACE_S:g})"
    )
    return parser


def _normalise(argv: list[str] | None) -> list[str]:
    """`--run-id …` with no verb means `run`: every command already written down keeps working."""
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] not in VERBS and args[0] not in ("-h", "--help"):
        return ["run", *args]
    return args


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(_normalise(argv))
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", args.run_id):
        parser.error("invalid --run-id")
    run_dir = REPO_ROOT / "experiments" / args.run_id / "results"
    if args.command == "stop":
        return _stop(args.run_id, run_dir, args.grace_s)
    if args.command == "novelty":
        return _novelty(args, run_dir)
    return _run(args, parser)


def _stop(run_id: str, run_dir: Path, grace_s: float) -> int:
    """Report what was stopped, and when nothing was, say what the archive says instead of guessing."""
    if not run_dir.is_dir():
        print(f"pipeline: no run {run_id} in experiments/", file=sys.stderr)
        return 2
    report = stop_run(run_dir, run_id, grace_s=grace_s)
    print(json.dumps(report, indent=2))
    if not report["recorded"]:
        done = completed_phases(run_dir)
        print(
            f"pipeline: {run_id} recorded no live process; its state says "
            f"{len(done)} phase(s) completed: {', '.join(done) or 'none'}",
            file=sys.stderr,
        )
        return 1
    if report["survived"]:
        return 1
    return 0


def _novelty(args: argparse.Namespace, run_dir: Path) -> int:
    """The prior-art stage on a finished run's own knowledge base, which its context file names."""
    state_path, contexts = run_dir / "state.json", sorted(run_dir.glob("context*.json"))
    if not state_path.is_file() or not contexts:
        print(f"pipeline: no run {args.run_id} to assess in experiments/", file=sys.stderr)
        return 2
    if "synthesize" not in json.loads(state_path.read_text()).get("completed", []):
        print(
            f"pipeline: {args.run_id} has not finished (`synthesize` is not completed); its hypotheses may "
            "still change",
            file=sys.stderr,
        )
        return 2
    kb_root = Path(json.loads(contexts[0].read_text())["kb"])
    config = load_config(args.config)
    orchestrator = Orchestrator(config, kb_root, run_dir, args.run_id, ClaudeLauncher(config, kb_root))
    return _assess_novelty(orchestrator, run_dir, again=args.again)


def _assess_novelty(orchestrator: Orchestrator, run_dir: Path, *, again: bool = False) -> int:
    """The prior-art stage on a finished run, shared by the `novelty` verb and by `run`. Returns the exit code.

    A stage already `completed` in the state is not paid for again: `run_novelty` returns the stored report.
    """
    try:
        if again and (moved := orchestrator.archive_novelty_attempt()):
            print(f"pipeline: the previous attempt is kept in {moved}", file=sys.stderr)
        report = orchestrator.run_novelty()
    except (PhaseFailed, ValueError) as err:
        print(f"pipeline: {err}", file=sys.stderr)
        return 1
    # The diagnosis is written whatever the outcome: a stage that failed is the one that most needs it.
    from evaluation.novelty_diagnosis import write as write_diagnosis

    diagnosis = write_diagnosis(run_dir, REPO_ROOT / SEARCH_CACHE)
    print(
        f"pipeline: novelty diagnosis: {diagnosis['problems']} problem(s), see {run_dir / '13-novelty-diagnosis.md'}",
        file=sys.stderr,
    )
    print(
        json.dumps(
            {k: report.get(k) for k in ("status", "assessed_count", "sources_searched", "searched_at")}
            | {
                "verdicts": {
                    e["hypothesis_id"]: (e.get("novelty") or {}).get("verdict") or e.get("error")
                    for e in report.get("entries", [])
                }
            },
            indent=2,
        )
    )
    return 0 if report.get("status") == "assessed" else 1


def _run(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    phases = tuple(p for p in args.phases.split(",") if p)
    unknown = set(phases) - set(PHASES)
    if unknown:
        parser.error(f"unknown phases {sorted(unknown)}")

    config = load_config(args.config)
    run_dir = REPO_ROOT / "experiments" / args.run_id / "results"
    run_dir.mkdir(parents=True, exist_ok=True)
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True).stdout.strip()
    context = {
        "run_id": args.run_id,
        "git_commit": commit,
        "config": str(args.config),
        "config_hash": config.config_hash,
        "kb": str(args.kb.resolve()),
        "phases": phases,
    }
    # One context file per invocation: a resumed run keeps the record of how it started.
    previous = sorted(run_dir.glob("context*.json"))
    name = "context.json" if not previous else f"context.resume-{len(previous)}.json"
    (run_dir / name).write_text(json.dumps(context | {"config_change": args.config_change}, indent=2))

    kb_root = args.kb.resolve()
    orchestrator = Orchestrator(
        config, kb_root, run_dir, args.run_id, ClaudeLauncher(config, kb_root), config_change=args.config_change
    )
    # Written before the first phase and removed on the way out: `stop` has no other way to find this run,
    # and a file left behind is how a crashed run is told apart from a finished one.
    write_pid_file(
        run_dir,
        RUN_PID_FILE,
        "orchestrator",
        os.getpid(),
        os.getpgid(0),
        extra={"run_id": args.run_id, "phases": list(phases)},
    )
    try:
        state = orchestrator.run(phases)
        # Inside the `try` so that `stop` still finds the run while the stage is paid. The run is complete without
        # the stage: a stage that fails (typically the cost cap, which it shares with the phases) is reported and
        # does not turn a finished run into a failed one.
        if "synthesize" in state["completed"] and not args.no_novelty:
            if _assess_novelty(orchestrator, run_dir) != 0:
                print(
                    f"pipeline: novelty was not assessed; the run itself is complete. Retry with "
                    f"`python -m pipeline.cli novelty --id {args.run_id}` (raise `run_max_cost_usd` if the cap was the reason).",
                    file=sys.stderr,
                )
    except MissingSource as err:
        # A missing input is the most likely first failure for a new user, and a traceback is the least
        # useful way to say it: the message already names every absent source and how to obtain it.
        print(f"pipeline: run {args.run_id} stopped in phase 'data'.\n{err}", file=sys.stderr)
        return 1
    except (PhaseFailed, ValueError) as err:
        print(f"pipeline: {err}", file=sys.stderr)
        return 1
    finally:
        clear_pid_file(run_dir, RUN_PID_FILE)
    print(json.dumps(state, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
