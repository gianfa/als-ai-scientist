# `agents/`

The roles of the discovery pipeline: one file per phase, `<phase>.md`, holding the exact instructions the agents of
that phase receive. `docs/agents.md` maps each role to its phase.

A `{{name}}` in a file is a slot the phase fills at run time with a value it computes (the objective, the slice of the
knowledge base, the tool blocks). The text is inlined into the phase workflow by `node workflows/build_workflow.mjs`,
so after editing a role, rebuild; `tests/workflow/prompts-golden.test.js` then shows every change of wording, and
`UPDATE_GOLDEN=1` accepts it. A role's settings (model, number of agents, budgets) are in `configs/pipeline.yaml`
under `phases.<phase>`.
