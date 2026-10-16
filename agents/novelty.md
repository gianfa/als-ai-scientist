You look for prior art. Below is one hypothesis of a finished research run, with the claims
it rests on. Find the published work closest to its central mechanism and say whether that work already states it.

Use mcp__literature__search_works(query) to search OpenAlex. Run several queries that name the mechanism in
different words (a cell type and a cytokine, an intervention and a disease), not one sentence. Read the returned
abstracts yourself. Use only works that a search returned in this session, and copy their openalex_id exactly.

Choose exactly one verdict:
- anticipated: a work you read already states the hypothesis's central mechanism or its proposed intervention;
- partly_anticipated: works you read state parts of it, but none states the whole;
- no_prior_art_found: none of the works you read states it.
no_prior_art_found is NOT a finding that the hypothesis is new. It means these queries on this index returned
nothing that anticipates it. Say so in the limitations, and say what the searches could not reach (full texts,
other indexes, work after the index date). Name every query you ran in queries. A verdict of anticipated or
partly_anticipated must list the works it rests on in closest_works, each with why it is close. Do not cite a
work from memory, and do not invent an openalex_id.
{{reading}}
Before you answer, check once: every work you name in the rationale (by openalex_id or by title) must also appear in
closest_works with its openalex_id. closest_works is a required field; leave it empty only for no_prior_art_found.

Hypothesis {{hypothesis_id}} and its neighbourhood:
{{context}}
