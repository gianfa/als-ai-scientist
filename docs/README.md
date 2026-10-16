# Documentation

Project documentation:

- [Quick start](quick-start.md) — prerequisites, end-to-end execution and outputs.
- [Project reference](reference.md) — repository map, configuration and entry points.
- [Agents](agents.md) — the roles of the pipeline, phase by phase, and where their instructions and settings live.
- [Observer UI](tools/observer-ui.md) — the local web UI: menu, run selector, and the Overview / Workflow / Story / Tokens / Evaluation views.
- [Testing](testing.md) — test suites, commands and the live probes of single phases.
- [Literature tools](tools/literature.md) — how papers are stored, fetched and quote-checked without an
  LLM: the store and **which store a run opens**, the integrity reports, and the quote check.
- [Drug lookup](tools/drug-lookup.md) — the opt-in check that a drug name proposed by `intervene` matches a catalogued
  compound (MyChem.info): what it returns, where it runs, how to read it.
- [Post-run evaluation](tools/evaluation.md) — read-only scores and audits of a saved run.
- [The discovery pipeline](pipeline.md) — what `./run.sh` launches: the orchestrator, the ten phases, the knowledge
  base, [the two causal graphs](pipeline.md#two-causal-graphs-and-why-they-are-not-the-same-object) and
  [how to draw one](pipeline.md#drawing-the-graph), budgets and limits.
- [Reading a mechanism out of the knowledge base](mechanism-views.md) — the four views over causal
  claims (`mechanism_graph`, `causal_edges`, `mechanism_path`, `mechanism_subgraph`), what the two
  filter axes mean, which view to hand a reviewer, and why the longest path is one selection rule
  among several.
- [The two registered analyses](analyses.md) — what `hla_panel_compact_v1` and `ics_cytokine_skew_v1`
  ask, what they take in and give out, how each works and what it does and does not show about ALS,
  written for a reader without an immunology background, with academic references.
- [How an experiment gets chosen, bought and run](experiment-selection.md) — `design` proposes, the host's
  data-feasibility check judges, `arbitrate` decides (informed by feasibility, not gated by it), `execute`
  runs only a bought, registered handler; what `design`/`arbitrate` are structurally blinded to and why; the
  two live probes that watch each step without a full run.
- [Licensing and release boundary](licensing.md).
