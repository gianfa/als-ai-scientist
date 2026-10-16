"""ClaudeLauncher with the subprocesses replaced: the command it builds and how it reads the outcome."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline import launcher as mod
from pipeline.config import load_config
from pipeline.launcher import ClaudeLauncher, PhaseFailed


class FakeProcess:
    pid = 4242

    def wait(self):
        return 0


@pytest.fixture
def fake_cli(monkeypatch, tmp_path):
    seen: dict = {"popen": [], "run": []}
    outcome = {
        "status": "completed",
        "result": {"phase": "literature", "outputs": [], "dropped": []},
        "cost_usd": 0.12,
        "agent_count": 4,
    }

    def run(cmd, **kw):
        seen["run"].append(cmd)
        if "workflows/inject_config.mjs" in cmd:
            Path(cmd[cmd.index("--out") + 1]).write_text("// injected")
        if "pipeline/extract_workflow_result.py" in cmd:
            stage = cmd[cmd.index("--stage") + 1]
            (Path(cmd[3]) / f"{stage}-result.json").write_text(json.dumps(outcome))
            return type(
                "R", (), {"returncode": 0 if outcome["status"] == "completed" else 1, "stdout": "", "stderr": ""}
            )()
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    def popen(cmd, **kw):
        seen["popen"].append(cmd)
        seen.setdefault("env", []).append(kw.get("env") or {})
        seen.setdefault("cwd", []).append(kw.get("cwd"))
        if "claude" in cmd:  # the launcher's stream: two result messages
            kw["stdout"].write(
                json.dumps({"type": "result", "usage": {"output_tokens": 300, "cache_read_input_tokens": 40}})
                + "\n"
                + json.dumps({"type": "result", "usage": {"output_tokens": 100}})
                + "\n"
            )
        return FakeProcess()

    monkeypatch.setattr(mod.subprocess, "run", run)
    monkeypatch.setattr(mod.subprocess, "Popen", popen)
    return seen, outcome


def test_one_claude_session_per_phase_with_read_only_kb_tools(fake_cli, tmp_path):
    seen, _ = fake_cli
    run = ClaudeLauncher(load_config(), tmp_path / "kb").run("literature", 1, {"phase": "literature"}, tmp_path)
    claude = next(c for c in seen["popen"] if "claude" in c)
    allowed = claude[claude.index("--allowedTools") + 1].split()
    assert "Workflow" in allowed and "mcp__kb__kb_search" in allowed and "Bash" not in allowed
    assert claude[claude.index("--disallowedTools") + 1] == "Bash Edit Write NotebookEdit"
    # --tools is the exclusive built-in set: Grep, Glob, Task and the rest are not callable at all.
    tools = claude[claude.index("--tools") + 1].split(",")
    assert tools == ["Workflow", "ToolSearch", "Read", "WebSearch", "WebFetch"]
    assert not {"Grep", "Glob", "Task", "Skill", "SendMessage", "Bash"} & set(tools)
    assert any("cost_watch.py" in " ".join(c) for c in seen["popen"])
    mcp = json.loads((tmp_path / "workflows" / "01-literature.mcp.json").read_text())
    assert mcp["mcpServers"]["kb"]["args"][-1] == str(tmp_path / "kb")
    literature = mcp["mcpServers"]["literature"]["args"]
    assert str(tmp_path / "literature-calls.agents.jsonl") in literature
    # `--max-fetch` is the grant for `fetch_paper`, so it carries this phase's own budget.
    assert literature[literature.index("--max-fetch") + 1] == "8"
    assert "mcp__literature__fetch_paper" in allowed
    assert "mcp__literature__check_quote" in allowed
    # The data server is registered only once the `data` phase has written its manifest.
    assert "data" not in mcp["mcpServers"] and "mcp__data__data_list" in allowed
    assert run.result["phase"] == "literature"
    assert run.launch["session_cost_usd"] == 0.12 and run.launch["stage"] == "01-literature"
    assert run.launch["launcher_usage"]["output_tokens"] == 400
    assert run.launch["launcher_usage"]["cache_read_input_tokens"] == 40
    assert json.loads((tmp_path / "workflows" / "01-literature.args.json").read_text()) == {"phase": "literature"}


def test_the_phase_session_is_confined_to_its_own_stage_directory(fake_cli, tmp_path):
    """A `Read` outside the working directory is denied, so the working directory is the reach.

    It holds this stage's script and nothing else: `mcp.json`, every other phase's `*.args.json` and the rest of
    the repository sit outside it. One directory per stage, not one per run, because the transcript directory
    under `~/.claude/projects/` is keyed by the cwd.
    """
    seen, _ = fake_cli
    ClaudeLauncher(load_config(), tmp_path / "kb").run("review", 5, {"try": 1}, tmp_path)
    stage_dir = tmp_path / "workflows" / "05-review"
    index = next(n for n, cmd in enumerate(seen["popen"]) if "claude" in cmd)
    assert seen["cwd"][index] == stage_dir
    assert [p.name for p in sorted(stage_dir.iterdir())] == ["05-review.js"]
    claude = seen["popen"][index]
    assert str(stage_dir / "05-review.js") in claude[claude.index("-p") + 1]
    # Out of reach, one level up.
    assert (tmp_path / "workflows" / "05-review.args.json").is_file()
    assert (tmp_path / "workflows" / "05-review.mcp.json").is_file()


def test_a_second_attempt_gets_its_own_stage_directory(fake_cli, tmp_path):
    """The collision check is the stage directory now, so a retry cannot overwrite the first attempt's script."""
    seen, _ = fake_cli
    launcher = ClaudeLauncher(load_config(), tmp_path / "kb")
    launcher.run("review", 5, {"try": 1}, tmp_path)
    launcher.run("review", 5, {"try": 2}, tmp_path)
    assert (tmp_path / "workflows" / "05-review" / "05-review.js").is_file()
    assert (tmp_path / "workflows" / "05-review.attempt2" / "05-review.attempt2.js").is_file()
    assert seen["cwd"][-2] == tmp_path / "workflows" / "05-review.attempt2"


def test_a_workflow_that_did_not_complete_fails_the_phase(fake_cli, tmp_path):
    _, outcome = fake_cli
    outcome.update(status="failed", result=None, error="schema")
    with pytest.raises(PhaseFailed, match="did not complete"):
        ClaudeLauncher(load_config(), tmp_path / "kb").run("literature", 1, {}, tmp_path)


def test_the_cost_cap_stops_the_run(fake_cli, tmp_path):
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "cost-cap-hit.json").write_text("{}")
    with pytest.raises(PhaseFailed, match="cost cap"):
        ClaudeLauncher(load_config(), tmp_path / "kb").run("literature", 1, {}, tmp_path)


@pytest.mark.parametrize(
    "phase, allowed, denied",
    [
        ("literature", {"WebSearch", "WebFetch"}, set()),
        ("review", {"WebSearch", "WebFetch"}, set()),
        ("generate", set(), {"WebSearch", "WebFetch"}),
        ("verify", set(), {"WebSearch", "WebFetch"}),
    ],
)
def test_web_tools_follow_the_phase_budget(fake_cli, tmp_path, phase, allowed, denied):
    seen, _ = fake_cli
    ClaudeLauncher(load_config(), tmp_path / "kb").run(phase, 1, {}, tmp_path)
    claude = next(c for c in seen["popen"] if "claude" in c)
    allow = set(claude[claude.index("--allowedTools") + 1].split())
    deny = set(claude[claude.index("--disallowedTools") + 1].split())
    assert allow & {"WebSearch", "WebFetch"} == allowed and deny & {"WebSearch", "WebFetch"} == denied
    # A phase without web budget cannot call the web tools even if it ignores its prompt.
    assert set(claude[claude.index("--tools") + 1].split(",")) & {"WebSearch", "WebFetch"} == allowed


def test_the_session_search_cap_is_set_from_the_phase_budget(fake_cli, tmp_path):
    seen, _ = fake_cli
    ClaudeLauncher(load_config(), tmp_path / "kb").run(
        "review", 1, {"web": {"searches": 4, "session_searches": 16}}, tmp_path
    )
    claude_env = seen["env"][next(i for i, c in enumerate(seen["popen"]) if "claude" in c)]
    assert claude_env["CLAUDE_CODE_MAX_WEB_SEARCHES_PER_SESSION"] == "16"


def test_a_second_attempt_of_a_phase_keeps_the_first_attempts_files(fake_cli, tmp_path):
    launcher = ClaudeLauncher(load_config(), tmp_path / "kb")
    first = launcher.run("review", 3, {"try": 1}, tmp_path)
    second = launcher.run("review", 3, {"try": 2}, tmp_path)
    assert (first.launch["stage"], second.launch["stage"]) == ("03-review", "03-review.attempt2")
    assert json.loads((tmp_path / "workflows" / "03-review.args.json").read_text()) == {"try": 1}
    assert (tmp_path / "03-review.attempt2-output.jsonl").exists()


def test_a_failed_injection_fails_the_phase(fake_cli, tmp_path, monkeypatch):
    def run(cmd, **kw):
        return type("R", (), {"returncode": 1, "stdout": "", "stderr": "already exists"})()

    monkeypatch.setattr(mod.subprocess, "run", run)
    with pytest.raises(PhaseFailed, match="config injection failed: already exists"):
        ClaudeLauncher(load_config(), tmp_path / "kb").run("review", 3, {}, tmp_path)


def test_the_data_server_is_registered_once_the_manifest_exists(fake_cli, tmp_path):
    (tmp_path / "data-manifest.json").write_text('{"sources": [], "absent": []}')
    ClaudeLauncher(load_config(), tmp_path / "kb").run("generate", 2, {"phase": "generate"}, tmp_path)
    mcp = json.loads((tmp_path / "workflows" / "02-generate.mcp.json").read_text())
    assert mcp["mcpServers"]["data"]["args"][-2:] == ["--manifest", str(tmp_path / "data-manifest.json")]


def test_the_session_budget_is_what_is_left_of_the_run_cap(fake_cli, tmp_path):
    """`--max-budget-usd` is the run's cap minus what the run already cost, so the CLI can only stop a session
    once the whole run has reached the ceiling, not on a per-phase cap with most of the budget unspent."""
    seen, _ = fake_cli
    config = load_config()
    (tmp_path / "03-literature-result.json").write_text(json.dumps({"cost_usd": 1.9350291}))
    (tmp_path / "04-generate-result.json").write_text(json.dumps({"cost_usd": 1.1507247}))
    (tmp_path / "05-review-result.json").write_text(json.dumps({"cost_usd": 3.0393356, "status": "stopped"}))
    run = ClaudeLauncher(config, tmp_path / "kb").run("review", 5, {}, tmp_path)
    claude = next(c for c in seen["popen"] if "claude" in c)
    expected = config.run_max_cost_usd - 6.1250894
    assert claude[claude.index("--max-budget-usd") + 1] == f"{expected:.2f}"
    assert run.launch["session_budget_usd"] == round(expected, 2)


def test_a_run_that_has_spent_its_cap_does_not_launch_the_phase(fake_cli, tmp_path):
    seen, _ = fake_cli
    config = load_config()
    (tmp_path / "03-literature-result.json").write_text(json.dumps({"cost_usd": config.run_max_cost_usd - 0.1}))
    with pytest.raises(PhaseFailed, match="left of the run cap"):
        ClaudeLauncher(config, tmp_path / "kb").run("review", 4, {}, tmp_path)
    assert not any("claude" in c for c in seen["popen"])


def test_the_spend_of_a_run_ignores_stages_whose_cost_is_unreadable(tmp_path):
    (tmp_path / "01-data-result.json").write_text(json.dumps({"cost_usd": 0.5}))
    (tmp_path / "02-compute-result.json").write_text("{not json")
    (tmp_path / "03-literature-result.json").write_text(json.dumps({"cost_usd": None}))
    (tmp_path / "04-generate-result.json").write_text(json.dumps({"status": "failed"}))
    (tmp_path / "04-generate.attempt2-result.json").write_text(json.dumps({"cost_usd": 0.25}))
    assert mod.run_spend_usd(tmp_path) == pytest.approx(0.75)
    assert mod.session_budget_usd(tmp_path, 1.0) == pytest.approx(0.25)
    assert mod.session_budget_usd(tmp_path, 0.5) == 0.0  # never negative


def _journal(path: Path, started: list[tuple[str, str]], results: dict[str, dict]) -> None:
    """A workflow journal: one `started` event per agent (id, label), one `result` event per finished agent."""
    lines = [json.dumps({"type": "launched"})]
    lines += [json.dumps({"type": "started", "agentId": a, "label": label}) for a, label in started]
    lines += [json.dumps({"type": "result", "agentId": a, "result": r}) for a, r in results.items()]
    lines.append("{not json")  # a truncated line must not lose the rest
    path.write_text("\n".join(lines) + "\n")


def test_the_results_a_stopped_workflow_had_finished_are_read_from_its_journal(tmp_path):
    """Agents that delivered a complete verdict before the phase was stopped must not be thrown away just
    because the workflow never reached its `return`."""
    journal = tmp_path / "05-review-journal.jsonl"
    _journal(
        journal,
        [("a1", "reviewer-1@H-17"), ("a2", "reviewer-2@H-17"), ("a3", "reviewer-1@H-18")],
        {"a3": {"summary": "the load-bearing step is contradicted", "claims": [{"ref": "e1"}]}},
    )
    outputs, lost = mod.salvage_outputs(journal)
    assert [o["agent_id"] for o in outputs] == ["reviewer-1@H-18"]
    assert outputs[0]["hypothesis_id"] == "H-18"  # the per-hypothesis label carries it
    assert outputs[0]["output"]["summary"].startswith("the load-bearing step")
    assert lost == ["reviewer-1@H-17", "reviewer-2@H-17"]
    # Nothing to salvage is not an error, and a label without `@` carries no hypothesis.
    assert mod.salvage_outputs(tmp_path / "absent.jsonl") == ([], [])
    plain = tmp_path / "03-literature-journal.jsonl"
    _journal(plain, [("b1", "literature-1")], {"b1": {"claims": []}})
    outputs, lost = mod.salvage_outputs(plain)
    assert outputs == [{"agent_id": "literature-1", "output": {"claims": []}}] and lost == []


def test_a_stopped_workflow_is_salvaged_instead_of_losing_the_phase(fake_cli, tmp_path):
    _, outcome = fake_cli
    outcome.update(status="stopped", result=None, error="Budget limit reached")
    _journal(
        tmp_path / "03-review-journal.jsonl",
        [("a1", "reviewer-1@H-7"), ("a2", "reviewer-2@H-7")],
        {"a1": {"summary": "s", "claims": []}},
    )
    run = ClaudeLauncher(load_config(), tmp_path / "kb").run("review", 3, {}, tmp_path)
    assert run.result["phase"] == "review" and run.result["salvaged"] is True
    assert [o["agent_id"] for o in run.result["outputs"]] == ["reviewer-1@H-7"]
    assert run.result["dropped"] == ["reviewer-2@H-7"]  # the orchestrator reports it as a dropped agent
    assert run.launch["salvaged_agents"] == ["reviewer-1@H-7"] and run.launch["lost_agents"] == ["reviewer-2@H-7"]
    assert run.launch["workflow_error"] == "Budget limit reached"


def test_a_stopped_workflow_with_nothing_finished_still_fails_the_phase(fake_cli, tmp_path):
    _, outcome = fake_cli
    outcome.update(status="stopped", result=None, error="Budget limit reached")
    _journal(tmp_path / "03-review-journal.jsonl", [("a1", "reviewer-1@H-7")], {})
    with pytest.raises(PhaseFailed, match="did not complete"):
        ClaudeLauncher(load_config(), tmp_path / "kb").run("review", 3, {}, tmp_path)


def test_the_paper_server_is_registered_when_the_extraction_is_present(fake_cli, tmp_path, monkeypatch):
    """The agents' only route to the paper's text, read-only, and absent when the file is."""
    extraction = tmp_path / "paper.md"
    extraction.write_text("# Paper\n\nSome prose.\n")
    monkeypatch.setattr(mod, "EXTRACTION", extraction)
    seen, _ = fake_cli
    ClaudeLauncher(load_config(), tmp_path / "kb").run("literature", 1, {}, tmp_path)
    mcp = json.loads((tmp_path / "workflows" / "01-literature.mcp.json").read_text())
    assert mcp["mcpServers"]["paper"]["args"][-2:] == ["--paper", str(extraction)]
    claude = next(c for c in seen["popen"] if "claude" in c)
    allowed = claude[claude.index("--allowedTools") + 1].split()
    assert set(mod.PAPER_TOOLS) <= set(allowed)


def test_no_paper_server_when_the_extraction_is_absent(fake_cli, tmp_path, monkeypatch):
    """A clone does not carry the extraction: a run without it has no paper tools, not a broken server."""
    monkeypatch.setattr(mod, "EXTRACTION", tmp_path / "missing" / "paper.md")
    ClaudeLauncher(load_config(), tmp_path / "kb").run("literature", 1, {}, tmp_path)
    mcp = json.loads((tmp_path / "workflows" / "01-literature.mcp.json").read_text())
    assert "paper" not in mcp["mcpServers"]


def test_a_judging_phase_is_not_given_the_paper_at_all(fake_cli, tmp_path, monkeypatch):
    """`review` judges claims. Registration is the grant, so the server must be absent from its
    config, not merely left out of `--allowedTools`, which only pre-approves."""
    extraction = tmp_path / "paper.md"
    extraction.write_text("# Paper\n\nSome prose.\n")
    monkeypatch.setattr(mod, "EXTRACTION", extraction)
    seen, _ = fake_cli
    ClaudeLauncher(load_config(), tmp_path / "kb").run("review", 5, {}, tmp_path)
    mcp = json.loads((tmp_path / "workflows" / "05-review.mcp.json").read_text())
    assert "paper" not in mcp["mcpServers"] and "kb" in mcp["mcpServers"]
    claude = next(c for c in seen["popen"] if "claude" in c)
    allowed = claude[claude.index("--allowedTools") + 1].split()
    assert not set(mod.PAPER_TOOLS) & set(allowed)


def test_only_a_phase_with_a_budget_gets_the_fetch_tool(fake_cli, tmp_path):
    """The grant is `--max-fetch`, not `--allowedTools`: the literature server is registered for every phase,
    so a phase without a budget must not see `fetch_paper` at all — `verify` above all."""
    seen, _ = fake_cli
    launcher = ClaudeLauncher(load_config(), tmp_path / "kb")
    budgets = {}
    for step, phase in enumerate(("literature", "review", "verify", "generate"), start=1):
        seen["popen"].clear()
        launcher.run(phase, step, {"phase": phase, "run_id": "RUN-1"}, tmp_path)
        mcp = json.loads((tmp_path / "workflows" / f"{step:02d}-{phase}.mcp.json").read_text())
        args = mcp["mcpServers"]["literature"]["args"]
        claude = next(c for c in seen["popen"] if "claude" in c)
        allowed = claude[claude.index("--allowedTools") + 1].split()
        budgets[phase] = (
            args[args.index("--max-fetch") + 1],
            "mcp__literature__fetch_paper" in allowed,
            args[args.index("--run-id") + 1],
        )
    assert budgets["literature"] == ("8", True, "RUN-1")
    assert budgets["review"] == ("4", True, "RUN-1")
    assert budgets["verify"] == ("0", False, "RUN-1")
    assert budgets["generate"] == ("0", False, "RUN-1")


def test_only_the_novelty_stage_gets_the_search_tool(fake_cli, tmp_path):
    """Same grant as `fetch_paper`: `--max-search` on the literature server, and the pre-approval only where it is
    above zero. No phase of the pipeline declares a search budget, so none of them sees `search_works`."""
    seen, _ = fake_cli
    launcher = ClaudeLauncher(load_config(), tmp_path / "kb")
    grants = {}
    for step, phase in enumerate(("literature", "verify", "novelty"), start=1):
        seen["popen"].clear()
        launcher.run(phase, step, {"phase": phase, "run_id": "RUN-1"}, tmp_path)
        mcp = json.loads((tmp_path / "workflows" / f"{step:02d}-{phase}.mcp.json").read_text())
        args = mcp["mcpServers"]["literature"]["args"]
        claude = next(c for c in seen["popen"] if "claude" in c)
        allowed = claude[claude.index("--allowedTools") + 1].split()
        grants[phase] = (args[args.index("--max-search") + 1], "mcp__literature__search_works" in allowed)
    assert grants == {"literature": ("0", False), "verify": ("0", False), "novelty": ("8", True)}


def test_the_search_grant_is_per_agent_times_the_agents_of_the_stage(fake_cli, tmp_path):
    """One server serves every agent of the stage. A grant of 8 for three agents was spent by two of them in
    `pl-prod-013` and left the third nothing to read, so the stage grants 8 for each hypothesis it launches."""
    seen, _ = fake_cli
    launcher = ClaudeLauncher(load_config(), tmp_path / "kb")
    launcher.run("novelty", 1, {"phase": "novelty", "run_id": "RUN-1", "items": [{}, {}, {}]}, tmp_path)
    mcp = json.loads((tmp_path / "workflows" / "01-novelty.mcp.json").read_text())
    args = mcp["mcpServers"]["literature"]["args"]
    assert args[args.index("--max-search") + 1] == "24"


def test_the_novelty_stage_gets_search_fetch_and_read_per_hypothesis_and_no_other_stage_does(fake_cli, tmp_path):
    seen, _ = fake_cli
    launcher = ClaudeLauncher(load_config(), tmp_path / "kb")
    grants = {}
    for step, phase in enumerate(("literature", "novelty"), start=1):
        seen["popen"].clear()
        launcher.run(phase, step, {"phase": phase, "run_id": "R", "items": [{}, {}, {}]}, tmp_path)
        mcp = json.loads((tmp_path / "workflows" / f"{step:02d}-{phase}.mcp.json").read_text())
        args = mcp["mcpServers"]["literature"]["args"]
        claude = next(c for c in seen["popen"] if "claude" in c)
        allowed = claude[claude.index("--allowedTools") + 1].split()
        grants[phase] = (
            args[args.index("--max-search") + 1],
            args[args.index("--max-read") + 1],
            args[args.index("--max-fetch") + 1],
            "mcp__literature__read_paper" in allowed,
        )
    assert grants["novelty"] == ("24", "9", "9", True)
    assert grants["literature"] == ("0", "0", "8", False)  # a stage with its own budget is not multiplied
