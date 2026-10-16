# Testing Guide

The suites run without credentials or API spend: agents are mocked, and nothing below calls a model except the
live probes, which say so and are opt-in.

## Quick Start

In a clean checkout, run `scripts/dev/docker-verify.sh` first: it builds the devcontainer image and runs the syntax
check, `uv sync --frozen`, the Python suite and the workflow suites inside it. It requires Docker, no credentials
and no API budget, and it does not launch `./run.sh`. Details: [devcontainer.md](devcontainer.md).

| Test type | Command |
|-----------|---------|
| **Python suite** | `UV_PROJECT_ENVIRONMENT=.venv uv run --frozen python -m pytest -q` |
| **Phase workflows, mocked runtime** | `node --test tests/workflow/*.test.js` |
| **Python syntax check** | `scripts/dev/python-syntax-check.sh` |
| **Build drift** | `node workflows/build_workflow.mjs --check` |
| **Lint, format, secrets** | `scripts/dev/code-quality.sh` |
| **`reassess` in isolation (live, paid)** | `ALS_LIVE_REASSESS=1 … pytest tests/test_reassess_probe.py -k live` |
| **`execute` in isolation (live, paid)** | `ALS_LIVE_EXECUTE=1 … pytest tests/test_execute_probe.py -k live` |

> **Note on the Python command.** Both `UV_PROJECT_ENVIRONMENT=.venv` and `python -m` matter inside this
> devcontainer, which exports another project's `UV_PROJECT_ENVIRONMENT` and a `VIRTUAL_ENV` that lacks
> the project's dependencies. Elsewhere a plain `uv run pytest` is fine.

## Phase workflows (Node.js)

`tests/workflow/phases.test.js` checks that each built phase in `workflows/phases/` matches its source, and runs
each phase script in process with mocked agents (`tests/workflow/harness.js::runWorkflow`): which agents are
launched, with which prompt and which arguments, and how invalid arguments are refused.
`tests/workflow/inject-config.test.js` covers the per-run injection of the configuration into a built phase.

## `reassess` in Isolation (One Live Phase)

`reassess` runs only after `execute` has verified an experiment, and a full run gets there only when the arbiter
buys an experiment with a registered handler, which no run guarantees. So the phase is tested alone, on delivery
cases kept in `tests/fixtures/reassess/`: each case is an execute report and a run ID over a minimal knowledge base
and an experiment artifact, versioned with the tests. Nothing reads an archived run or the shared store.

```bash
# free: each case is a valid delivery, each refusal has its reason, a fake reassessor judges every case
UV_PROJECT_ENVIRONMENT=.venv uv run --frozen python -m pytest -q tests/test_reassess_probe.py

# paid: a live reassessor per case (about 0.40 USD each, capped at 2 USD per case)
ALS_LIVE_REASSESS=1 UV_PROJECT_ENVIRONMENT=.venv uv run --frozen python -m pytest -s -q \
    tests/test_reassess_probe.py -k live --basetemp=experiments/probe-reassess-live-<UTC>
```

One case can also be launched directly, which keeps its directory under `experiments/`:

```bash
UV_PROJECT_ENVIRONMENT=.venv uv run --frozen python -m pipeline.reassess_probe \
    tests/fixtures/reassess/cases/repeated_execution.json
```

The probe first checks the case against what `reassess` reads (status `executed`, a bare digest equal to the
verified one, result claims present in the knowledge base, dropped IDs absent from it, a current hypothesis of
that run) and writes nothing when a check fails. It then runs the phase under the case's run ID in a fresh
directory, with a copy of the case's knowledge base, an empty literature store of its own and its own spend cap.
Exit 0 on `judged`, 1 otherwise, 2 on an invalid case. A new edge case is a new file in
`tests/fixtures/reassess/cases/`; `tests/fixtures/reassess/README.md` says how.

## `execute` in Isolation (One Live Phase)

`execute` runs only after the arbiter buys an EXP naming a registered analysis handler
(`analysis/experiment_runner.py::HANDLERS`), and no run guarantees that — one production run bought six EXPs and none
named one, so `execute` never launched an agent at all. The phase is tested alone, on delivery cases kept in
`tests/fixtures/execute/`: each case names a minimal knowledge base already holding a bought `Decision` and a
matching `Experiment`, and the local sources that `Experiment` declares. Nothing reads an archived run or the
shared store. Unlike `reassess`'s live test, `execute`'s live agent makes a real tool call: it runs the real
registered handler on the case's own synthetic data, so a live test here exercises the handler end to end too.

```bash
# free: each case is selectable as written, refusals have their reason, a fake executor calls the real tool
UV_PROJECT_ENVIRONMENT=.venv uv run --frozen python -m pytest -q tests/test_execute_probe.py

# paid: a live executor per case (one tool call against a synthetic deposit, capped at 2 USD per case)
ALS_LIVE_EXECUTE=1 UV_PROJECT_ENVIRONMENT=.venv uv run --frozen python -m pytest -s -q \
    tests/test_execute_probe.py -k live --basetemp=experiments/probe-execute-live-<UTC>
```

One case can also be launched directly, which keeps its directory under `experiments/`:

```bash
UV_PROJECT_ENVIRONMENT=.venv uv run --frozen python -m pipeline.execute_probe \
    tests/fixtures/execute/cases/ics_cytokine_skew_v1.json
```

The probe first checks the case with the real `select()`/`assess()` functions — the same ones `execute` itself
calls — so it cannot select an EXP the phase would refuse, and writes nothing when the check fails. It then runs
the phase under the case's run ID in a fresh directory, with a copy of the case's knowledge base, a built data
manifest and feasibility row, an empty literature store of its own and its own spend cap. `execute` writes its
own `execution-policy.json`; the probe never pre-writes one. Exit 0 on `executed` with a faithful interpretation,
1 otherwise, 2 on an invalid case. A new edge case (for example, `hla_panel_compact_v1`) is a new file in
`tests/fixtures/execute/cases/` naming a different knowledge base and sources, no code change needed;
`tests/fixtures/execute/README.md` says how.

## `design` + `arbitrate` in Isolation (Two Live Phases, No Forced Outcome)

Neither `reassess_probe` nor `execute_probe` observes whether an agent *chooses* a registered handler: both
start from a `Decision`/`Experiment` already written into the fixture. `pipeline/design_arbitrate_probe.py`
isolates the step before both: proposal and purchase, with nothing pre-written. A case's knowledge base holds
one hypothesis with an unresolved target claim and nothing else; its data manifest is built from only one
registered handler's inputs, never a second's — a `design` agent cannot propose the other handler as
`data_supported` because the data for it is not declared, not because the prompt forbids it.
`author_confidence` is irrelevant here and cannot be used to bias the outcome: it is blinded from every agent
(`knowledge_base/retrieve.py::BLINDED`).

**This probe does not, and cannot, guarantee a specific choice.** A real `design` agent may propose a wet-lab
experiment instead or none; a real `arbiter` may reject or stop. It reports what happened and asserts only what
is true by construction (e.g. a bought EXP cannot be more `data_supported` than what was proposed), never a
judgement call.

```bash
# free: the deterministic plumbing, with fixed design/arbitrate outputs
UV_PROJECT_ENVIRONMENT=.venv uv run --frozen python -m pytest -q tests/test_design_arbitrate_probe.py

# paid: a live design agent, then a live arbiter, reported honestly (about 0.5-0.6 USD total)
ALS_LIVE_DESIGN_ARBITRATE=1 UV_PROJECT_ENVIRONMENT=.venv uv run --frozen python -m pytest -s -q \
    tests/test_design_arbitrate_probe.py -k live --basetemp=experiments/probe-design-arbitrate-live-<UTC>
```

Run twice on 2026-10-09, before and after the `metadata_fields` fix (`tools/data/cytometry.py`,
`pipeline/experiment_feasibility.py`) was merged in: **before**, the live designer wrote free prose for
`required_metadata`, exactly reproducing a production run's `unverified` result independently; **after**, the same
kind of live designer named the inventory's own keys, resolved `data_supported`, and the arbiter bought it —
`handler_bought: true`. Both runs' archives are under `experiments/probe-design-arbitrate-live-*/`.

## Running All Tests

```bash
scripts/dev/python-syntax-check.sh
UV_PROJECT_ENVIRONMENT=.venv uv run --frozen python -m pytest -q
node --test tests/workflow/*.test.js
node workflows/build_workflow.mjs --check
scripts/dev/code-quality.sh
```

Stop on the first failure unless you want a full report. The git hooks (`.pre-commit-config.yaml`) run the last
two on every commit and merge.

## Notes

- **No external dependencies in the default suites:** mocked agents, fixtures and temporary directories only.
- **Live probes** need a `claude` login and spend money; each states its cap.
- **Run archives:** a run's journals and reports are under `experiments/<RUN_ID>/results/`.
