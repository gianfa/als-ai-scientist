"""Read-only post-run evaluation of saved run archives.

See docs/tools/evaluation.md.

The tools one reaches for first, by the stage they look at. Each is run as `python -m evaluation.<name>` with
`UV_PROJECT_ENVIRONMENT=.venv uv run --frozen` in front, on `experiments/<RUN>/results`; its own docstring gives the
exact arguments. Utilities (`citations`, `local`, `provenance`) are not listed.

Whole run
- `run_score`: the measurements a pre-registration is judged against, one run.
- `scorecard`: one row per evaluation dimension, `no scorer` where there is none.
- `figures`: diagnostic figures drawn from the persisted scorecards.

literature, verify
- `quote_outcomes`: what `check_quote` said per stage, with the fetch attempts beside it.
- `sources`: the scientific sources a run read.

Any stage
- `tool_usage`: which tools the agents of a run actually called.
- `claim_trace`: who wrote one claim, with which tools, and what its quote check said.

intervene, synthesize
- `citation_audit`: what each citation of the final proposal rests on, and what a human must still read.
- `proposal_readable`: the final proposal as a document readable without the knowledge base.

Knowledge base
- `kb_stats`: statistics over the knowledge base and the run archives.
"""
