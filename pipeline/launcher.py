"""How a phase workflow is run. The orchestrator only sees the `Launcher` protocol.

`ClaudeLauncher` follows the conventions of `run.sh`: the args are injected into a per-run copy of the built phase
workflow (`workflows/inject_config.mjs`), one `claude -p` session calls the Workflow tool on it, the
whole-run cost watcher (`pipeline/cost_watch.py`) runs next to it, and the outcome is read from the
background-task events (`pipeline/extract_workflow_result.py`). It also records what the launch itself costs: the
launcher session's own `total_cost_usd` (the workflow's agents are not in it) and its wall-clock time, which is the
measurement asked for before deciding whether to merge phases. The stream's `total_cost_usd` covers the whole
session, workflow agents included, so the launcher's own share is taken from the `usage`
of its `result` messages, which count only the launcher's turns.

**The session budget is what is left of the run's cap**, not a separate per-phase figure: `--max-budget-usd` does
stop the workflow's background agents, so a per-phase cap below the run cap can
only kill a phase that the run could still afford, losing everything that phase had spent without writing it to the
knowledge base. Here
`run_max_cost_usd` minus what the run already cost (`run_spend_usd`, the CLI's own per-session figures, attempts and
resumes included) is handed to every session, which makes the CLI enforce the same ceiling the watcher estimates.

`FakeLauncher` returns canned results, for tests and dry runs.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .config import PACKAGE_ROOT, PipelineConfig
from .stop import PHASE_PID_FILE, clear_pid_file, write_pid_file

# `PACKAGE_ROOT` is the repository root.
REPO_ROOT = PACKAGE_ROOT
READ_TOOLS = (
    "mcp__kb__kb_get",
    "mcp__kb__kb_neighbourhood",
    "mcp__kb__kb_search",
    "mcp__kb__kb_mechanism_graph",
    "mcp__kb__kb_mechanism_path",
    "mcp__literature__check_quote",
    "mcp__data__data_list",
    "mcp__data__data_describe",
)
# The paper tools are not in `READ_TOOLS`: they are granted to two phases only. A phase
# that judges claims must not be able to re-read the paper, or the verifier is doing the author's job. The grant
# is made by **registering the server or not**, per phase, because `--allowedTools` only pre-approves and an MCP
# tool is not covered by the exclusive `--tools` set: a server present in the config is callable.
PAPER_TOOLS = ("mcp__paper__paper_navigate", "mcp__paper__paper_passage")
PAPER_PHASES = ("literature", "generate")
EXPERIMENT_TOOLS = ("mcp__experiment__run_experiment",)
# `fetch_paper` is on the same literature server as `check_quote`, which is registered for every phase, so the
# grant cannot be this list: it is `--max-fetch`, below, which decides whether the tool is registered at all.
# This name only pre-approves it for the phases that have a budget.
LITERATURE_FETCH_TOOLS = ("mcp__literature__fetch_paper",)
# Same grant for `search_works`: `--max-search` above zero registers it, and only the novelty stage declares one.
LITERATURE_SEARCH_TOOLS = ("mcp__literature__search_works",)
LITERATURE_READ_TOOLS = ("mcp__literature__read_paper",)
# Stages whose literature grants are per agent but served by one server: the grant is the per-agent figure times the
# agents the stage launches (one per `items` entry). A grant shared as it stood was spent by two agents of three.
SHARED_SERVER_STAGES = ("novelty",)
# The Paper2Agent extraction, served read-only by `tools.paper.mcp_server`.
# A path, not a config entry, because there is exactly one extraction and it is local-only material: a run
# without it registers no `paper` server rather than a broken one.
EXTRACTION = REPO_ROOT / "dist/michaelis-2025-agent/skill/michaelis-2025-paper/references/paper.md"
# The built-in tools a phase session may use at all. `--allowedTools` only pre-approves: with `--permission-mode
# dontAsk` every other built-in stays callable (e.g. `Grep` on the repository).
# `--tools` makes the set exclusive. `ToolSearch` is required, because the
# agents load the MCP schemas through it, and `Read` is required, because the Workflow tool refuses a scriptPath it
# cannot read. MCP tools are not affected by `--tools`.
#
# `--tools` alone does NOT leave the MCP servers as the only route to data. `Read` reaches every file the session
# may read, and with `cwd=REPO_ROOT` that would be the whole repository: `dist/`, `data/`, and
# `experiments/<RUN_ID>/results/*.jsonl`, where `agent_id` appears and the blinding `review` depends on could be
# stepped around.
#
# What bounds the reach is the working directory: under `--permission-mode dontAsk` a `Read` outside the cwd is
# denied. Each phase session therefore runs in its own stage directory
# holding nothing but its workflow script (`run()` below), which the Workflow tool requires to be inside the cwd
# or an added directory. One directory per stage, not one per run, because the transcript directory under
# `~/.claude/projects/` is keyed by the cwd: a shared cwd would let a `review` agent read the `generate` phase's
# transcripts, authorship included.
BUILTIN_TOOLS = ("Workflow", "ToolSearch", "Read")
# Below this, a session is not worth launching: the phase would be stopped mid-flight and write nothing.
MIN_SESSION_BUDGET_USD = 0.5


class PhaseFailed(RuntimeError):
    """The phase workflow did not complete (or the cost cap stopped it)."""


@dataclass
class PhaseRun:
    result: dict[str, Any]
    launch: dict[str, Any] = field(default_factory=dict)  # cost/time of the launch itself, paths


def launcher_usage(stream: Path) -> dict[str, int]:
    """Tokens of the launcher's own turns: the sum of the `usage` of the stream's `result` messages."""
    keys = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")
    total = dict.fromkeys(keys, 0)
    for line in stream.read_text().splitlines():
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if message.get("type") == "result":
            for key in keys:
                total[key] += int((message.get("usage") or {}).get(key) or 0)
    return total


def run_spend_usd(run_dir: Path) -> float:
    """What the run has actually cost so far, from the CLI's own figures.

    Each `<stage>-result.json` holds the launched session's `total_cost_usd`, which covers the workflow's agents.
    Summed over every stage file present in the run directory, so re-attempts and resumes count: a resumed run
    continues the run's budget instead of restarting it from zero.
    A stage whose cost is missing or unreadable contributes nothing and is not an error; the watcher
    (`pipeline/cost_watch.py`) estimates the same quantity independently.
    """
    total = 0.0
    for path in sorted(run_dir.glob("*-result.json")):
        try:
            cost = json.loads(path.read_text()).get("cost_usd")
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        if isinstance(cost, (int, float)) and cost > 0:
            total += float(cost)
    return total


def salvage_outputs(journal: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """The agent results a workflow had already produced, rebuilt from its journal: `(outputs, lost_labels)`.

    A phase workflow persists nothing by itself. The orchestrator sees only the single value the workflow returns
    (`workflows/phases/src/lib/phase.js`: `{phase, outputs: [{agent_id, hypothesis_id?, output}], dropped}`), so a
    workflow killed before its `return` — by a budget, a timeout or a crash — discards every agent that had already
    finished, though each was paid for.

    The journal the extractor copies next to the stream holds a `started` event per agent, with its label, and a
    `result` event per finished agent. That is enough to rebuild the contract: the finished agents become
    `outputs`, in the order they started, and the rest are reported as dropped, exactly as a workflow that lost
    them would have reported. Per-hypothesis phases label their agents `<role>@<hypothesis_id>`, which is where
    the `hypothesis_id` tag comes from.

    It recovers what exists; it never invents an output, and an empty result means there is nothing to salvage.
    """
    if not journal.is_file():
        return [], []
    labels: dict[str, str] = {}
    order: list[str] = []
    results: dict[str, Any] = {}
    for line in journal.read_text().splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        agent_id = event.get("agentId")
        if not isinstance(agent_id, str):
            continue
        if event.get("type") == "started":
            if agent_id not in labels:
                order.append(agent_id)
            labels[agent_id] = event.get("label") or agent_id
        elif event.get("type") == "result" and isinstance(event.get("result"), dict):
            results[agent_id] = event["result"]  # a retried agent keeps its last result
    outputs: list[dict[str, Any]] = []
    lost: list[str] = []
    for agent_id in order:
        label = labels[agent_id]
        if agent_id in results:
            entry: dict[str, Any] = {"agent_id": label, "output": results[agent_id]}
            if "@" in label:
                entry["hypothesis_id"] = label.rsplit("@", 1)[1]
            outputs.append(entry)
        else:
            lost.append(label)
    return outputs, lost


def session_budget_usd(run_dir: Path, cap_usd: float) -> float:
    """What is left of the run's cap, for `claude --max-budget-usd`. Never negative."""
    return max(0.0, cap_usd - run_spend_usd(run_dir))


class Launcher(Protocol):
    def run(self, phase: str, step: int, args: dict[str, Any], run_dir: Path) -> PhaseRun: ...


class FakeLauncher:
    """`responses[phase](args) -> workflow result`. Records every call."""

    def __init__(self, responses: dict[str, Callable[[dict[str, Any]], dict[str, Any]]]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def run(self, phase: str, step: int, args: dict[str, Any], run_dir: Path) -> PhaseRun:
        self.calls.append((phase, args))
        return PhaseRun(self.responses[phase](args), {"fake": True})


class ClaudeLauncher:
    def __init__(self, config: PipelineConfig, kb_root: Path) -> None:
        self.config, self.kb_root = config, kb_root

    def _web_tools(self, phase: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """(allowed, disallowed) web tools for a phase: a tool with a zero budget is not available at all."""
        budget = self.config.web_budget(phase)
        allowed = tuple(t for t, n in (("WebSearch", budget.searches), ("WebFetch", budget.fetches)) if n > 0)
        return allowed, tuple(t for t in ("WebSearch", "WebFetch") if t not in allowed)

    def _mcp_config(self, run_dir: Path, phase: str, stage: str, run_id: str, agents: int = 1) -> Path:
        """Read-only servers: the KB; the quote check on the literature store (its own audit log, apart from the
        orchestrator's provenance log, so the agents' checks and the final check can be compared); and the run's
        data manifest, which serves metadata only. The data server is registered only once the `data` phase has
        written its manifest, so a phase run without it simply has no data tools rather than a broken server.
        The `paper` server is registered only for the phases of `PAPER_PHASES`, and only when the extraction is
        present: it is local-only material that a clone does not carry. One config per stage, not one per run,
        because registration is what grants an MCP tool."""
        width = max(agents, 1) if phase in SHARED_SERVER_STAGES else 1

        def server(module: str, *module_args: str) -> dict:
            return {
                "command": "uv",
                "args": ["run", "--quiet", "--directory", str(PACKAGE_ROOT), "python", "-m", module, *module_args],
                "env": {"UV_PROJECT_ENVIRONMENT": ".venv", "VIRTUAL_ENV": ""},
            }

        servers = {
            "kb": server("knowledge_base.mcp_server", "--kb", str(self.kb_root)),
            # `--max-fetch` is the grant for `fetch_paper`: above zero the server registers it, and the
            # phase's agents can bring a paper they have identified into the store. Zero for every
            # phase that does not declare `agent_fetch_max_new`, `verify` among them.
            "literature": server(
                "tools.literature.mcp_server",
                "--store",
                str(self.config.store_path()),
                "--log",
                str(run_dir / "literature-calls.agents.jsonl"),
                "--max-fetch",
                str(self.config.agent_fetch_budget(phase) * width),
                "--max-search",
                str(self.config.agent_search_budget(phase) * width),
                "--max-read",
                str(self.config.agent_read_budget(phase) * width),
                "--run-id",
                run_id,
            ),
        }
        manifest = run_dir / "data-manifest.json"
        if manifest.is_file():
            servers["data"] = server("tools.data.mcp_server", "--manifest", str(manifest))
        if phase in PAPER_PHASES and EXTRACTION.is_file():
            servers["paper"] = server("tools.paper.mcp_server", "--paper", str(EXTRACTION))
        if phase == "execute":
            policy = run_dir / "execution-policy.json"
            if not policy.is_file():
                raise PhaseFailed("execute: the host has not written an execution policy")
            servers["experiment"] = server("tools.experiment.mcp_server", "--policy", str(policy))
        path = run_dir / "workflows" / f"{stage}.mcp.json"
        path.write_text(json.dumps({"mcpServers": servers}, indent=2))
        return path

    def run(self, phase: str, step: int, args: dict[str, Any], run_dir: Path) -> PhaseRun:
        workflows = run_dir / "workflows"
        # A re-attempt of a phase (after a failure) never overwrites the files of the earlier attempt.
        stage = f"{step:02d}-{phase}"
        attempt = 1
        while (workflows / stage).exists() or (run_dir / f"{stage}-output.jsonl").exists():
            attempt += 1
            stage = f"{step:02d}-{phase}.attempt{attempt}"
        logs = run_dir / "logs"
        workflows.mkdir(parents=True, exist_ok=True)
        logs.mkdir(parents=True, exist_ok=True)
        args_path = workflows / f"{stage}.args.json"
        # The session's working directory, and so the limit of what `Read` reaches: this stage's script and
        # nothing else. `mcp.json` and every `*.args.json` stay in `workflows/`, one level up and out of reach.
        stage_dir = workflows / stage
        stage_dir.mkdir(parents=True, exist_ok=True)
        script = stage_dir / f"{stage}.js"
        args_path.write_text(json.dumps(args, indent=1))
        inject = subprocess.run(
            [
                "node",
                "workflows/inject_config.mjs",
                "--workflow",
                f"workflows/phases/{phase}.js",
                "--args",
                str(args_path),
                "--out",
                str(script),
                "--manifest",
                str(workflows / f"{stage}.manifest.json"),
            ],
            cwd=PACKAGE_ROOT,
            capture_output=True,
            text=True,
        )
        if inject.returncode != 0:
            raise PhaseFailed(f"{stage}: config injection failed: {inject.stderr.strip() or inject.stdout.strip()}")
        prompt = (
            f'Call the Workflow tool exactly once with scriptPath set to "{script}" and no args. '
            "Do not read, write or modify any files and do not call any other tool.\n"
            "The workflow runs in the background: wait until you are notified that it has finished "
            "(completed or failed), then reply with one word, done, and stop."
        )
        (logs / f"{stage}.launcher-prompt.txt").write_text(prompt)
        stream = run_dir / f"{stage}-output.jsonl"
        web_allowed, web_denied = self._web_tools(phase)
        launcher = self.config.launcher
        # What is left of the run's cap, not a per-phase figure: see the module docstring.
        budget = session_budget_usd(run_dir, self.config.run_max_cost_usd)
        if budget < MIN_SESSION_BUDGET_USD:
            raise PhaseFailed(
                f"{stage}: only {budget:.2f} USD left of the run cap "
                f"(RUN_MAX_COST_USD={self.config.run_max_cost_usd}); raise it to go on"
            )
        env = {**os.environ, "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS": str(launcher.timeout_s * 1000)}
        # Built-in session search cap (CLI >= 2.1.212): counts the workflow's agents too; past it, WebSearch returns
        # a notice telling the agent to go on with what it has. Set to the phase's per-agent budget x its agents.
        searches = int((args.get("web") or {}).get("session_searches") or 0)
        if searches > 0:
            env["CLAUDE_CODE_MAX_WEB_SEARCHES_PER_SESSION"] = str(searches)
        command = [
            "timeout",
            "--kill-after=5s",
            str(launcher.timeout_s),
            "claude",
            "-p",
            prompt,
            "--session-id",
            str(uuid.uuid4()),
            "--model",
            launcher.model,
            "--output-format",
            "stream-json",
            "--permission-mode",
            "dontAsk",
            "--mcp-config",
            str(self._mcp_config(run_dir, phase, stage, str(args.get("run_id") or ""), len(args.get("items") or []))),
            "--tools",
            ",".join((*BUILTIN_TOOLS, *web_allowed)),
            "--allowedTools",
            " ".join(
                (
                    "Workflow",
                    *web_allowed,
                    *READ_TOOLS,
                    *(PAPER_TOOLS if phase in PAPER_PHASES else ()),
                    *(LITERATURE_FETCH_TOOLS if self.config.agent_fetch_budget(phase) > 0 else ()),
                    *(LITERATURE_SEARCH_TOOLS if self.config.agent_search_budget(phase) > 0 else ()),
                    *(LITERATURE_READ_TOOLS if self.config.agent_read_budget(phase) > 0 else ()),
                    *(EXPERIMENT_TOOLS if phase == "execute" else ()),
                )
            ),
            "--disallowedTools",
            " ".join(("Bash", "Edit", "Write", "NotebookEdit", *web_denied)),
            "--max-budget-usd",
            f"{budget:.2f}",
            "--verbose",
        ]
        started = time.monotonic()
        with (
            open(stream, "w") as out,
            open(logs / f"{stage}.launcher.log", "w") as err,
            open(logs / "cost-watch.log", "a") as watch_log,
        ):
            process = subprocess.Popen(command, stdout=out, stderr=err, env=env, cwd=stage_dir)
            # The paid session, written down while it lives. Killing the orchestrator's group can leave this
            # process orphaned with no cost watcher, i.e. spending with no cap; `pipeline.cli stop` can only
            # reach it if it is on record (`pipeline/stop.py`).
            write_pid_file(run_dir, PHASE_PID_FILE, "session", process.pid, None, extra={"stage": stage})
            watch = subprocess.Popen(
                [
                    "uv",
                    "run",
                    "--quiet",
                    "python",
                    "pipeline/cost_watch.py",
                    str(run_dir),
                    "--pid",
                    str(process.pid),
                    "--cap-usd",
                    str(self.config.run_max_cost_usd),
                ],
                cwd=PACKAGE_ROOT,
                env={**os.environ, "UV_PROJECT_ENVIRONMENT": ".venv", "VIRTUAL_ENV": ""},
                stdout=watch_log,
                stderr=subprocess.STDOUT,
            )
            try:
                exit_code = process.wait()
                watch.wait()
            finally:
                clear_pid_file(run_dir, PHASE_PID_FILE)
        wall_s = round(time.monotonic() - started, 1)
        if (logs / "cost-cap-hit.json").is_file():
            raise PhaseFailed(f"{stage}: run cost cap reached (RUN_MAX_COST_USD={self.config.run_max_cost_usd})")
        extract = subprocess.run(
            [
                "python3",
                "pipeline/extract_workflow_result.py",
                str(stream),
                str(run_dir),
                "--stage",
                stage,
                "--evaluated-stage",
                "none",
                "--exit-code",
                str(exit_code),
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        (logs / f"{stage}.extract.log").write_text(extract.stdout + extract.stderr)
        record = json.loads((run_dir / f"{stage}-result.json").read_text())
        launch = {
            "stage": stage,
            "launcher_exit": exit_code,
            "wall_s": wall_s,
            "session_budget_usd": round(budget, 2),  # the cap this session was launched under
            "session_cost_usd": record.get("cost_usd"),
            "launcher_usage": launcher_usage(stream),
            "agent_count": record.get("agent_count"),
            "workflow_status": record.get("status"),
            "workflow_usage": record.get("usage"),
            # Which tools the phase's agents actually called (read from their transcripts by the extractor).
            "agent_tools": record.get("agent_tools") or {},
        }
        if extract.returncode != 0 or not isinstance(record.get("result"), dict):
            # The workflow never returned. Whatever its agents had finished is still in the journal: take it
            # rather than pay for all of them again, and say in the record that the phase is a salvage.
            outputs, lost = salvage_outputs(run_dir / f"{stage}-journal.jsonl")
            if not outputs:
                raise PhaseFailed(f"{stage}: workflow did not complete ({record.get('status')}: {record.get('error')})")
            launch |= {
                "salvaged": True,
                "salvaged_agents": [o["agent_id"] for o in outputs],
                "lost_agents": lost,
                "workflow_error": record.get("error"),
            }
            return PhaseRun({"phase": phase, "outputs": outputs, "dropped": lost, "salvaged": True}, launch)
        return PhaseRun(record["result"], launch)
