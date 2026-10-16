# Project reference

A compact map of the code and of what a run reads and writes. How a run proceeds, phase by phase, is in
[pipeline.md](pipeline.md).

## Repository map

| Path | Responsibility |
| --- | --- |
| `run.sh` | Entry point: a thin forwarder to `python -m pipeline.cli`. |
| `pipeline/` | The orchestrator: phase order, state, resume, gates, launches, cost cap. |
| `workflows/phases/` | One Workflow script per agent phase, built from `workflows/phases/src/` (edit the sources, then rebuild). |
| `agents/` | The instructions of each role, one file per phase; see [agents.md](agents.md). |
| `knowledge_base/` | The append-only knowledge base: schemas, store, status computed from relations, and its MCP server. |
| `tools/` | Narrow tools: literature store and quote check, data sources, the paper extraction, the graph builder, experiment execution. |
| `analysis/` | Deterministic analyses and the registered experiment handlers, with artifact verification. |
| `evaluation/` | Read-only post-run scores and audits over a run's archive. |
| `observability/` | The local observer UI and token accounting. |
| `configs/` | Run configuration (`pipeline*.yaml`) and token pricing (`tokens.yaml`). |
| `policies/` | The paper usage policy (`papers.md`). |
| `scripts/` | Scripts by purpose: `setup/` (environment), `data/` (data and literature acquisition), `ops/` (observer and run inspection), `dev/` (quality and reproducibility checks). Nothing a run needs at run time lives here. |
| `tests/` | Python suite, and the Node workflow tests under `tests/workflow/`. |
| `experiments/<RUN_ID>/results/` | The archive of each run (git-ignored). |
| `results/`, `artifacts/` | Working output outside any run's archive (git-ignored). |
| `dist/` | The local Paper2Agent extraction of the source article; never distributed. |
| `docs/` | User documentation. |

## Configuration and entry points

- `configs/pipeline.yaml` is the configuration of a production run: objective, launcher model and timeout, the
  whole-run spend cap (`run_max_cost_usd`), the data sources, and per-phase settings under `phases.<phase>`.
  `configs/pipeline-smoke.yaml` is the cheap configuration of the first run.
- `./run.sh --run-id <RUN_ID> [--config FILE]` runs the phases in order and resumes after the last completed one
  when the same `--run-id` is given again.
- `node workflows/build_workflow.mjs` rebuilds every phase workflow from its source; `--check` fails when a built
  file is out of date.
- `python -m pipeline.cli novelty` runs the post-run novelty stage on a finished run.
- `analysis/cli.py` runs a deterministic analysis directly, outside a run.

## Inputs and outputs

A run declares its inputs in `phases.data.sources`; the `data` phase resolves and hashes each one and fails
explicitly when a required input is missing ([data-acquisition.md](data-acquisition.md)). Each phase writes its
report (`<NN>-<phase>-report.json`) into the run's archive, and `state.json` records the phases that completed.
Claims, hypotheses, experiments and decisions live in the knowledge base, not in the run directory. A run that
reaches `synthesize` writes `final-proposal.json` and `final-proposal.md`. A run may end with
`insufficient_evidence`; proposed experiments are proposals unless an `execute` receipt says otherwise.
