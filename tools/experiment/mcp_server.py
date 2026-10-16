"""The only agent-callable experiment tool: run one host-authorized registered analysis.

The policy is written by the orchestrator after arbitration and names every EXP this phase may execute. The
agent supplies only an EXP ID; it cannot provide Python, parameters, source paths, an analysis ID or an output
path. The server is registered once per stage, not once per agent (`pipeline/launcher.py`), so when a phase
executes several experiments every one of its agents reaches this same authorized set; which agent ran which
is recorded by each artifact's own summary, and the host binds the artifact to its EXP.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from analysis.experiment_runner import HANDLERS, execute


def run_experiment(policy_path: Path, experiment_id: str) -> dict[str, Any]:
    try:
        policy = json.loads(policy_path.read_text())
        authorized = {entry["experiment_id"]: entry for entry in policy["selected"]}
        selected = authorized.get(experiment_id)
        if selected is None:
            return {
                "error": f"{experiment_id} is not one of the bought, supported EXPs selected for this "
                f"phase ({', '.join(sorted(authorized)) or 'none'})"
            }
        if selected["analysis_id"] not in HANDLERS or selected["feasibility"] != "data_supported":
            return {"error": "selected EXP has no registered, data-supported handler"}
        run_dir = policy_path.parent
        summary = execute(
            json.loads((run_dir / "data-manifest.json").read_text()),
            Path(policy["repository_root"]),
            run_dir / "experiment-analysis" / experiment_id,
            policy["run_id"],
            experiment_id,
            policy["config_hash"],
            selected["analysis_id"],
        )
        return {
            "experiment_id": experiment_id,
            "analysis_id": summary["analysis_id"],
            "outcome": summary["outcome"],
            "result_digest": summary["result_digest"],
            "summary_ref": f"experiment-analysis/{experiment_id}/summary.json",
            "receipt_ref": f"experiment-analysis/{experiment_id}/verification.json",
            "readout": HANDLERS[selected["analysis_id"]].readout(summary),
            "limitations": summary["limitations"],
        }
    except (OSError, ValueError, KeyError) as err:
        return {"error": f"{type(err).__name__}: {err}"}


def build_server(policy_path: Path):
    from mcp.server.mcpserver import MCPServer

    server = MCPServer("experiment")

    @server.tool(
        name="run_experiment",
        description="Execute a bought EXP authorized by this run's policy — the one named in your "
        "instructions. Takes only its EXP ID; returns a digest-bound result or an explicit "
        "error. No Python or file paths accepted.",
    )
    def run_tool(experiment_id: str) -> dict[str, Any]:
        return run_experiment(policy_path, experiment_id)

    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    try:
        policy = json.loads(args.policy.read_text())
        selected = policy.get("selected") or []
        if not isinstance(selected, list) or not selected:
            raise ValueError("policy selects no experiment")
        if unregistered := [
            entry.get("experiment_id") for entry in selected if entry.get("analysis_id") not in HANDLERS
        ]:
            raise ValueError(f"policy names unregistered analyses for {', '.join(map(str, unregistered))}")
        server = build_server(args.policy)
    except (OSError, ValueError, ImportError) as err:
        print(f"experiment MCP server: cannot start: {err}", file=sys.stderr)
        return 2
    if args.check:
        print("experiment MCP server: ok", file=sys.stderr)
        return 0
    server.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
