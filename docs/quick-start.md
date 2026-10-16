# Quick start

ALS AI Scientist is a prototype discovery system. A Python orchestrator runs one small Claude Code Dynamic Workflow per phase and is the only writer of a persistent, claim-based knowledge base; the phases are described in [pipeline.md](pipeline.md). No hypothesis, review or intervention this system has produced has been assessed by a domain expert.

> [!WARNING]
> Using Opus 5.5 could get your session flagged by the server-side safety classifier (category "bio"). That is why Sonnet is the default launcher model. If you switch to Opus anyway, any fallback is recorded in `context.json`. Model behaviour changes over time: check Anthropic's official documentation for the current situation (for example, [Why Claude switched models in your conversation](https://support.claude.com/en/articles/16049681-why-claude-switched-models-in-your-conversation-with-opus-5-or-opus-5-5)).

> [!TIP]
> `scripts/setup/prepare-environment.sh` does the steps below that can be scripted (dependencies, skill, links, GWAS and IEDB downloads) and lists the ones only you can do. `--dry-run` shows what it would do.

> [!NOTE]
> Contributors: `scripts/dev/code-quality.sh` runs ruff, black (check only) and detect-secrets; the git hooks call it. Install them once per clone with `.venv/bin/pre-commit install --hook-type pre-commit --hook-type pre-merge-commit`. On commit they check the staged files, on every merge the whole repository.

## Requirements

Before checking each requirement by hand, we strongly recommend using the image defined in [`.devcontainer/`](../.devcontainer/): it can be opened in VS Code as a [devcontainer](devcontainer.md).

- **Claude Code CLI** with Dynamic Workflows enabled, installed and available in `PATH`.
- **Python 3.10 or newer** and [uv](https://docs.astral.sh/uv/).
- **The declared data inputs.** `phases.data.sources` in the configuration names them; the `data` phase fails explicitly if a required one is missing. With `configs/pipeline.yaml` these are:
  - the **local Paper2Agent extraction** at `dist/michaelis-2025-agent/skill/michaelis-2025-paper` (two tables are read from `assets/table/`). Obtain the Michaelis et al. article from the [publisher](https://doi.org/10.1038/s41586-025-09588-6) (CC BY-NC-ND 4.0), then extract it with the command in [data-acquisition.md §1.1](data-acquisition.md#11-extracting-it-with-paper2agent) — the `--output` path and the default table asset names are what the configuration addresses — and check the result with `scripts/setup/check-paper2agent.sh`;
  - the **ALS GWAS summary statistics** (GCST90027164) under `data/external/gwas-als/`, acquired with
    `scripts/data/gwas-data-download.sh`;
  - the **IEDB/NetMHCIIpan prediction cache** under `data/external/iedb-cache/`, acquired once with
    `scripts/data/iedb-cache-acquire.py` under the DTU academic licence ([data-acquisition.md](data-acquisition.md));
  - the **Michaelis Zenodo cytometry deposit** under `data/external/michaelis-2025-zenodo/`, declared `required: false`: no analysis in this repository reads an `.fcs` file yet.

  One acquisition procedure per input, with its licence: [data-acquisition.md](data-acquisition.md) and [LICENSES.md](../LICENSES.md). These inputs are user-provided and never committed.
- Any credentials and network access required by the configured Claude models and by the literature tools.

Install the Python dependencies from the package directory:

```sh
UV_PROJECT_ENVIRONMENT=.venv uv sync
```

### Without uv (conda or pip)

`environment.yml` (conda) and `requirements.txt` (pip) install the same pinned runtime dependencies, exported
from `uv.lock`. They are a fallback, not the supported setup:

- `./run.sh` requires `uv` and runs `uv run`, which builds its own `.venv` and ignores a conda or pip
  environment. In such an environment, launch the pipeline directly from the repository root:
  `python -m pipeline.cli run --id <RUN_ID>`. A live run still needs the Claude Code CLI.
- `pytest` is not included: `pip install pytest`, then `python -m pytest -q`.
- The optional extras (`figures`, `causal` in `pyproject.toml`) are not included.

```sh
conda env create -f environment.yml && conda activate als-ai-scientist
# or
python -m pip install -r requirements.txt
```

## Run end to end

From the repository root:

```sh
./run.sh --run-id pl-008                                        # all phases, configs/pipeline.yaml
./run.sh --run-id pl-008 --config configs/pipeline-smoke.yaml   # the cheap variant
./run.sh --run-id pl-008 --phases data,compute                  # the two phases with no agent
./run.sh --help
```

**A live run is paid**: it launches one `claude -p` session per phase. `data` and `compute` have no agent and cost nothing.

`run.sh` is a thin shim that forwards every argument to `python -m pipeline.cli`; the orchestrator owns phase order, state, resume, provenance, budgets and termination, so there is no launcher logic to keep in step with it.

Launch it detached, so that losing the shell does not lose a paid run:

```sh
nohup ./run.sh --run-id pl-008 > experiments/pl-008-nohup.log 2>&1 & disown
```

## Stop a run

```sh
uv run python -m pipeline.cli stop --id pl-008
```

`stop` reads the pids the run wrote down — `run.pid` for the orchestrator and `phase.pid` for the phase's paid
session, both in `experiments/<run-id>/results/` — sends `SIGTERM` to the process group and to each pid, waits
`--grace-s` (10 s by default) and then `SIGKILL`s whatever survived. It prints what it did as JSON.

Two things it deliberately does not do. It never looks for the run **by name** in the process table: a
`pgrep -f "pipeline.cli --run-id pl-008"` matches the shell running that very `pgrep`. And it never signals a
recorded pid that no longer identifies itself as this run, since pids are reused. If a run recorded nothing,
`stop` says so and reports how many phases the archive says completed, rather than claiming to have stopped
something.

`run.sh` keeps one job, launching, which is the entry point; the verbs
(`run`, `stop`) live on the CLI. `--run-id` and `--id` are the same option, and a bare `--run-id …` with no
verb is read as `run`, so every command written down before this existed still works.

### Configuration

- `configs/pipeline.yaml` is the production configuration; `configs/pipeline-smoke.yaml` is the smallest and cheapest variant (fewer questions and authors, smaller models), selected with `--config`.
- Both set the objective, questions, perspectives, models, per-phase timeouts and every budget. The limits, how each one is enforced and what happens when it is reached are tabulated in [pipeline.md](pipeline.md#budgets-and-limits); the whole-run cap is `run_max_cost_usd`.
- The configuration is typed, validated and hashed; the hash is recorded with the run.
- The knowledge base is `knowledgebase/` at the repository root, git-ignored and shared by all runs.

### Resuming an interrupted run

Re-run with the same `--run-id`: the orchestrator resumes after the last completed phase, and each invocation writes its own `context*.json` so the record of how the run started is kept.

A resume must use the configuration the run started with. To resume with a changed configuration, pass `--config-change "<reason>"`; the old and new hashes, the reason and the phase are recorded in `state.json`.

## Outputs

Per run, under `experiments/<run-id>/results/`:

- `context.json` (run id, git commit, configuration path and hash, knowledge-base path, phases) and `state.json` (completed phases, declared configuration changes);
- per phase, the injected workflow and its arguments under `workflows/`, the session stream, `<NN>-<phase>-result.json` and `<NN>-<phase>-report.json` (written, seen again, rejected, invalid outputs, dropped elements and agents, `unresolved_sources` — the claims it wrote whose source names no published identifier — launch cost and wall time);
- `literature-calls.jsonl` (quote checks), `inherited-mechanism-graph.json` and `prior-causal-graph.json`;
- `transcripts/<stage>/agent-*.jsonl`: the agents' own transcripts, copied into the archive because the harness deletes them from its session storage, each with its size and sha256 recorded in `<NN>-<phase>-result.json` (`pipeline/extract_workflow_result.py --no-archive-transcripts` declines the copy);
- `final-proposal.json` and `final-proposal.md` when the run reaches synthesis, with the gate's report on the citations.

The claims, hypotheses, experiments, interventions and decisions themselves live in the shared knowledge base, not in the run directory. `experiments/` and `knowledgebase/` are git-ignored: results are not versioned.

The knowledge base is **shared across runs and append-only**: a run inherits everything earlier runs established, a claim already recorded is logged as `seen_again` rather than written twice, and nothing is ever edited or retracted. This is deliberate — see "What a run inherits from the ones before it" in `docs/pipeline.md` for what it buys and what it costs. To run in isolation, pass `--kb <empty-dir>`: the run then starts from nothing and leaves the shared store untouched.

A run may end in `insufficient_evidence`, which is a valid scientific outcome. Proposed experiments are proposals, not performed experiments, and this is a research system, not clinical decision support.

## Inspecting a run

```sh
uv run python -m tools.graph --kb knowledgebase --run pl-008 --format html --out graph.html
uv run python -m evaluation.tool_usage experiments/pl-008/results --format markdown
```

Deterministic components (configuration resolution, input digests, the `compute` analyses with their fixed seed) are reproducible from the source; the agent generations and live literature retrieval depend on model versions and on the state of external services.
