# Agents

`./run.sh` runs the discovery pipeline as a sequence of phases (`docs/pipeline.md`). Each phase that needs
judgement launches one Workflow whose agents play one role. A role is a set of instructions plus the settings it
runs with:

- **instructions**: in `agents/<phase>.md`, inlined into the phase workflow `workflows/phases/<phase>.js` by
  `node workflows/build_workflow.mjs`; a `{{name}}` is a value the phase fills at run time, and each run records the
  sha256 of the role files it used;
- **settings**: in `configs/pipeline.yaml`, under `phases.<phase>` (model, number of agents, per-agent budgets).

Agents write into the knowledge base only through its tools, and the host checks every output against its schema
before anything is stored.

## Roles

| Logical role | Phase | Agents | What it does |
|---|---|---|---|
| Evidence Agent (literature) | `literature` | one per research question (`literature.questions`) | Collects claims from papers, each with a source and a verbatim quote checked against the paper. |
| Hypothesis Generator | `generate` | one per perspective (`generate.perspectives`) | Independent authors write competing hypotheses from the knowledge-base slice. |
| Adversarial Critic | `review` | `review.reviewers` per hypothesis | Independent reviewers search for evidence against each hypothesis, and for it. |
| Independent Evidence / Citation Verifier | `verify` | one per batch (`verify.batch_size`) | Judges whether each relation is supported by its source. |
| Experiment / Falsification Designer | `design` | one per hypothesis | Proposes experiments that could falsify a hypothesis, with the data each one needs. |
| Arbiter | `arbitrate` | one per hypothesis | Chooses which proposed experiments to run, informed by their feasibility. |
| Experiment agent | `execute` | one per selected experiment | Runs a registered analysis on the data and reports its result. |
| Post-result critic | `reassess` | one per executed experiment | Judges what an experiment's result means for its hypothesis. |
| Intervention Agent | `intervene` | one per hypothesis | Proposes candidate interventions for a hypothesis. |
| PI / Synthesis Agent | `synthesize` | one | Writes the final proposal (sections A–H), citing only knowledge-base IDs. |

The `novelty` stage runs after a run, outside `PHASES`: one agent per hypothesis searches for prior art
(`python -m pipeline.cli novelty`).
