"""Command line entry point.

Run from the repository root:

    python -m observability.wf_observer.cli start [--name NAME]          # prints the new run ID
    python -m observability.wf_observer.cli log RUN_ID KIND [--agent A] [--data JSON] [--latency-ms N] ...
    python -m observability.wf_observer.cli runs
    python -m observability.wf_observer.cli events RUN_ID
    python -m observability.wf_observer.cli metrics RUN_ID
    python -m observability.wf_observer.cli serve [--port 8765] [--smoke-root PATH]  # UI at / (menu Agents + Run)
    python -m observability.wf_observer.cli tokens check <run_dir> [--config-file CONFIG]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import tokens as tokens_module
from ._config import DEFAULT_RUN_STORE, EXPERIMENTS_ROOT, SMOKE_ROOT
from .events import EVENT_KINDS, Event, InvalidEventError, new_run_id
from .metrics import summarize
from .server import serve
from .store import RunNotFoundError, RunStore, git_commit

# Absolute, from `_config`. A relative path would silently encode one working directory, and from any
# other the UI would list no experiment at all, without an error.
DEFAULT_ROOT = DEFAULT_RUN_STORE


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="observability")
    p.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="run store directory")
    sub = p.add_subparsers(dest="command", required=True)

    start = sub.add_parser("start", help="create a new run")
    start.add_argument("--name", default=None)

    log = sub.add_parser("log", help="append one event to a run")
    log.add_argument("run_id")
    log.add_argument("kind", choices=sorted(EVENT_KINDS))
    log.add_argument("--agent")
    log.add_argument("--data", default="{}", help="JSON object")
    log.add_argument("--latency-ms", type=float)
    log.add_argument("--input-tokens", type=int)
    log.add_argument("--output-tokens", type=int)
    log.add_argument("--cost-usd", type=float)

    sub.add_parser("runs", help="list runs")
    for name in ("events", "metrics"):
        sub.add_parser(name).add_argument("run_id")

    srv = sub.add_parser("serve", help="read-only UI and JSON API")
    srv.add_argument("--host", default="127.0.0.1")
    srv.add_argument("--port", type=int, default=8765)
    srv.add_argument("--smoke-root", type=Path, default=SMOKE_ROOT, help="smoke test results directory")
    srv.add_argument("--e2e-root", type=Path, default=EXPERIMENTS_ROOT, help="e2e experiments directory")

    tokens_sub = sub.add_parser("tokens", help="token usage commands")
    tokens_sub_cmd = tokens_sub.add_subparsers(dest="tokens_command", required=True)
    tokens_check = tokens_sub_cmd.add_parser("check", help="check token usage and write snapshot")
    tokens_check.add_argument("run_dir", type=Path, help="run output directory")
    tokens_check.add_argument("--config-file", type=Path, help="tokens.yaml config file")

    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    store = RunStore(args.root)
    try:
        if args.command == "start":
            run_id = new_run_id()
            store.create_run(run_id, {"name": args.name, "git_commit": git_commit()})
            store.append(Event(run_id=run_id, kind="run_started", data={"name": args.name}))
            print(run_id)
        elif args.command == "log":
            store.append(
                Event(
                    run_id=args.run_id,
                    kind=args.kind,
                    agent=args.agent,
                    data=json.loads(args.data),
                    latency_ms=args.latency_ms,
                    input_tokens=args.input_tokens,
                    output_tokens=args.output_tokens,
                    cost_usd=args.cost_usd,
                )
            )
        elif args.command == "runs":
            print("\n".join(store.list_runs()))
        elif args.command == "events":
            for event in store.read_events(args.run_id):
                print(json.dumps(event.to_dict(), ensure_ascii=False))
        elif args.command == "metrics":
            metrics = summarize(args.run_id, store.read_events(args.run_id))
            print(json.dumps(metrics.to_dict(), indent=2))
        elif args.command == "serve":
            serve(store, args.host, args.port, args.smoke_root, args.e2e_root)
        elif args.command == "tokens":
            if args.tokens_command == "check":
                return tokens_module.main(
                    [
                        "check",
                        str(args.run_dir),
                        *(["--config-file", str(args.config_file)] if args.config_file else []),
                    ]
                )
    except (InvalidEventError, RunNotFoundError, json.JSONDecodeError) as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
