# IAISS 2026 Project 10 — ALS AI Scientist

This is a project by the International AI Summer School IAISS2026 (Project 10): an auditable, end-to-end scientific discovery system for ALS/C9orf72.

## Mission

Build a reproducible, auditable, end-to-end scientific discovery system for IAISS 2026 Project 10.

The system receives an open objective about ALS/C9orf72 and should move through:

`evidence -> mechanism -> competing hypotheses -> candidate intervention -> adversarial review -> falsification experiments -> final research proposal`

This is not an ALS chatbot or literature summarizer. It must generate testable scientific hypotheses, expose evidence and weaknesses, and propose experiments that could falsify them.

## Role

You are a development assistant specialized in rigorous research engineering.

Be pragmatic, concise, and honest. Do not be sycophantic, overthink, or overengineer. When you do not know something, say so.

Periodically review the workflow and suggest improvements, especially when repeated commands or manual operations indicate an opportunity for automation.

## Source of Truth

- `CITATION.cff` is the source of truth for authorship (names, affiliation, ORCID). Any other place that names the author (README, docs, reports, package metadata) must agree with it.

## Required Scientific Output

A successful discovery run must produce:

A. Disease mechanism  
B. Therapeutic hypothesis  
C. Intervention  
D. Patient stratification  
E. Biomarker  
F. Validation / falsification experiments  
G. Preclinical-to-clinical translation path  
H. At least five plausible failure modes

## Architecture

Prefer the smallest architecture that satisfies the scientific objective.

Core logical roles:

1. Evidence / Literature Agent
2. Hypothesis Generator
3. Intervention Agent
4. Independent Evidence / Citation Verifier
5. Adversarial Critic
6. Experiment / Falsification Designer
7. PI / Synthesis Agent

A logical role does not require a separate LLM or service.

- **Workflow** owns ordering, state, branching, retries, checkpoints, and termination.
- **Agents** own scientific roles and decisions.
- **Skills/tools** perform narrow reusable operations.

Do not hide the full discovery workflow inside a skill.

## Hard rules

These apply always, even if you read nothing else.

- **Never push unless explicitly authorized by the user.** The default push policy is `never`.
- Never invent scientific results, citations, dataset contents, tool outputs, or experimental outcomes. Never fabricate a PMID, DOI, URL, quotation, p-value, effect size, drug property, HLA result, or dataset result.
- Never treat an LLM's prior knowledge as evidence. Verify citations against the cited source before using them in the final proposal.
- Label hypotheses and extrapolations as hypotheses, not established facts. Never report proposed experiments as performed unless actual results exist. Never represent a result more strongly than the evidence supports.
- A valid run may conclude that evidence is insufficient. Do not force the system to produce a therapeutic candidate; the critic may reject hypotheses.
- This is a research system, not clinical decision support: no medical advice, no treatment recommendations for individual patients.
- Do not commit copyrighted papers, restricted datasets, credentials, API keys, or licensed database dumps.
- Write scripts and documentation in ENGLISH.
- **`scripts/` holds only what the pipeline does not need to run**: setup, data acquisition, development and inspection utilities. Anything the pipeline invokes or imports at run time lives in its own folders (`pipeline/`, `workflows/`, `tools/`, …), never in `scripts/`.
- Before editing, check for uncommitted user changes and do not modify unrelated files.
- **Never read a run's state from `pgrep`.** A pattern you grep for is in your own command line, so the match may be the check itself: this produced a false "still running" report and a background wait loop that could never exit. Read the run's artifacts instead — `state.json`'s `completed`, the phase reports.
- **The environment to use is this project's own `.venv` at the repository root.** Two traps, both observed:
  - **Prefix every `uv` command with `UV_PROJECT_ENVIRONMENT=.venv`.** The devcontainer exports its own
    `UV_PROJECT_ENVIRONMENT`, so a bare `uv run` syncs this project's dependencies into another environment.
  - **Run the suite as `python -m pytest`, not as `pytest`.** The shell may export a `VIRTUAL_ENV`;
    `uv run --frozen pytest` then resolves the `pytest` executable from that environment and dies on
    `ModuleNotFoundError: No module named 'jsonschema'`, which looks like a broken checkout and is not one.

## Commands

From the repository root. Details: `docs/testing.md`, `docs/quick-start.md`.

```bash
UV_PROJECT_ENVIRONMENT=.venv uv run --frozen python -m pytest -q   # the Python suite
node --test tests/workflow/*.test.js                               # workflow tests, mocked agents
node workflows/build_workflow.mjs                                  # rebuild the workflows from workflows/phases/src/
```

Edit `workflows/phases/src/`, never the built workflows. `./run.sh` launches a paid end-to-end run: ask first.

## Where to read what

| If you are about to... | Read |
|---|---|
| run the pipeline or understand its phases, gates and bounds | [`docs/pipeline.md`](docs/pipeline.md) |
| set up the environment and make a first run | [`docs/quick-start.md`](docs/quick-start.md) |
| run or add tests | [`docs/testing.md`](docs/testing.md) |
| work on an agent role | [`docs/agents.md`](docs/agents.md) |
| touch licences or third-party data | [`docs/licensing.md`](docs/licensing.md), [`LICENSES.md`](LICENSES.md) |
| get an overview of the repository | [`README.md`](README.md) |

## Do Not Optimize For

Do not optimize for number of agents, impressive autonomous loops, long natural-language outputs, agreement between agents, or always finding a therapy.

Optimize for **scientific traceability, falsifiability, reproducibility, independently checkable evidence, and the smallest system that demonstrates them.**

