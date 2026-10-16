# `literature` probe fixtures

One case per shape of the question the phase is given. Each names a `run_id`, a `kb` directory (relative to the
case file) and the `questions` that replace the production ones; everything else comes from the production
config, because the prompt and the model are what the probe tests.

- `kb/` is **empty**. `literature` is handed no knowledge-base slice by design
  (`pipeline/orchestrator.py`: a clean room, so extraction is not steered by what is already recorded), so an
  empty base is the honest input and the phase writes the first records into it.
- The literature store is **not** a fixture: `pipeline/literature_probe.py::prepare` creates an empty one per
  probe run. That is the point of the probe. With no paper in the store every `check_quote` comes back
  `paper_not_in_store`, so the only route to a verified quote is `mcp__literature__fetch_paper` (ADR 0029),
  and whether a real agent takes that route is a fact about the agent that no offline test can establish.

What is real here: the agents, the MCP servers, the quote check, Europe PMC and OpenAlex. The paper is really
downloaded and the quote is really verified against the text that arrived. What is the probe's: the knowledge
base, the store, the questions and the spend cap.

The retry policy is **not** exercised: a 429 cannot be provoked on demand.
`tests/test_tools_literature_fetch.py` covers it offline, down to the real `urllib` client reading
`Retry-After` from a local server.

## Cases

- `fetch_a_paper_it_found`: one production question, one agent, an empty store. Exit 0 requires that an agent
  fetched at least one paper **and** that at least one claim of the run is `verified` — the chain the 85
  archived dead ends never completed.
