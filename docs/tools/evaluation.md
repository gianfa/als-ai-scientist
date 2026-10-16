# Post-run evaluation

The `evaluation` package (`evaluation/`) reads one saved run and reports on it. Everything in it
is deterministic and **read-only**: it never corrects outputs, reruns agents or edits the run directory.

The modules are `scorecard`, `run_score`, `citation_audit`, `claim_trace`, `quote_outcomes`, `refine_entry`,
`tool_usage` and `kb_stats`.

## The phase pipeline: the scorecard and the audit

```bash
uv run python -m evaluation.scorecard experiments/<RUN>/results [--audit <filled packet>] [--format json]
uv run python -m evaluation.citation_audit experiments/<RUN>/results --save
uv run python -m evaluation.citation_audit experiments/<RUN>/results --verdicts <filled packet>
uv run python -m evaluation.run_score experiments/<RUN>/results [--format markdown]
uv run python -m evaluation.refine_entry [--format markdown]
uv run python -m evaluation.tool_usage experiments/<RUN>/results [--diagnose]
uv run python -m evaluation.claim_trace experiments/<RUN>/results <CLAIM_ID> [--format json]
uv run python -m evaluation.quote_outcomes experiments/<RUN>/results [--format json]
uv run python -m evaluation.quote_outcomes --runs <RUN_ID> <RUN_ID>
```

**`scorecard`** prints one row per evaluation dimension, each with a value, the
artifact it was read from and the limit a reader must hold. It produces **no overall score**: the weights would
be ours and nobody could defend them. Three kinds of row, and the difference matters:

| row | meaning |
|---|---|
| *scored* | a number read from an artifact |
| `—` (null) | a scorer exists, its input is missing from this archive — the reason is in the row |
| **`no scorer`** | nothing honest can be computed from a run's archive, and the row says why |

Three dimensions carry `no scorer` (statistical robustness, biological plausibility, external validation). That is
deliberate: no favourable metric is invented, and a plausible number over the wrong denominator is worse than an
admission. The list lives in `evaluation/scorecard.py::NO_SCORER` with a reason each, and a test asserts it is still
there.

**Hypothesis diversity (EVAL-AI4)** used to be on that list: the obvious metric, overlap of the claim sets the
hypotheses were given, measures shared input context, not independence. `evaluation/scorecard.py::hypothesis_diversity`
asks a different, output-side question instead — whether two hypotheses assert distinct causal mechanisms, by
comparing the `(cause_ref, effect_ref)` edges of the claims each has `part_of` it (`evaluation/refine_entry.py::claims_of`),
not their text. Value is 1 minus the mean pairwise Jaccard similarity of those edge sets, over this run's
non-superseded hypotheses; a hypothesis with no causal claim of its own counts as fully similar to every other
such hypothesis, by convention, not by comparison — the row's `limit` field says so. Fewer than two live
hypotheses: `null`, not a fabricated number.

**Novelty (EVAL-S7, EVAL-AI3)** is scored from a stage that runs *after* a finished run:
`python -m pipeline.cli novelty --run-id <ID>` (paid: one session, one agent per live hypothesis). Each agent
searches OpenAlex through the `search_works` tool, reads the abstracts it gets, optionally opens the full text of the closest papers (below), and returns one verdict:
`anticipated`, `partly_anticipated` or `no_prior_art_found`, with the queries it ran and the works it relied on.
The host checks each work on its own: a work the search tool did not actually return is dropped from the entry, with the reason, and the verdict stands on the works that remain. The entry is refused only when a verdict of prior art is left with no served work at all, or when `no_prior_art_found` rests on queries that returned no work (an agent that read nothing found nothing). Every entry carries a `provenance` block: the works kept, the works dropped, the works named in the prose alone, and whether each kept work was read in full or in abstract. When the stage ends it writes `13-novelty-diagnosis.md`, which names what went wrong: an agent that never searched, queries it reports but the server never logged, searches refused for budget, works the host dropped, and a verdict left with nothing served. The result is `13-novelty-report.json`, and the
scorecard counts the verdicts; before the stage has run, the two rows read `—`, not zero. It reports the full
texts two ways, because two hypotheses may cite the same paper: `works_read_in_full` sums each hypothesis's own
count, so it says how deep each verdict goes, and `distinct_works_read_in_full` counts the works once.

**Reading full texts.** An abstract can leave out what a paper's body says, so the stage can let each agent open the
full text of a few papers: `phases.novelty.max_papers_read` in the configuration (default 3 per hypothesis; `0`
leaves the stage on abstracts alone). The agent fetches a paper into the literature store by DOI and reads it page by
page with `read_paper` (6 000 characters a page); a paper is counted the first time it is opened, and its later pages
are free. A paywalled paper cannot be read, and the agent says so. The host checks the agent's list of papers read
(`full_texts_read`) against the server's own log and refuses a paper it never served, so reading is the one claim
here that is not taken on trust. It also makes the join itself, per work and per agent: a kept work is `full_text` when the agent for that
hypothesis declared its DOI among the papers it read *and* the server's log of this stage confirms the paper was
served; everything else is `abstract`. Both halves are needed — the declaration, because `read_paper` logs the
paper and not who asked, so a paper another hypothesis's agent opened would otherwise be credited to this one;
the log, because the declaration alone is the agent's word. The mark can therefore only under-state the depth
(a paper read and not declared reads as `abstract`), never overstate it. What reading still does not do: it does not make the verdict a measurement. The
agent may read badly, and a paper it did not open can say what the hypothesis says.

What the row does **not** say: `no_prior_art_found` is not "this is new". It means these queries, on this index, on
that date, returned no work stating the hypothesis. Full texts, other indexes and later work are out of reach, and
the verdict is an agent's reading whose error rate nobody has measured. The stage is not one of the
twelve phases, but `./run.sh` launches it after `synthesize`, under the run's cost cap; `--no-novelty` leaves it out and
`python -m pipeline.cli novelty --id <ID>` runs it later. The search settings are under `phases.novelty` in the
configuration.

**`citation_audit`** does the mechanical half of the citation audit: it resolves every citation of a final
proposal, re-checks every quote against the stored paper (recording the `text_sha256` of the text it read, since
the store is mutable), and prints a **support ladder** over every citation tag — a quote checked, a quote whose
source text is absent, a quote naming no source, no quote at all, or a derived record. `--save` writes
`<RUN>/results/audit/citations.md`, whose verdict columns a **human** fills: whether each quoted claim
`supports`, `overstates`, is `unrelated`, or is `cited to limit` the proposal's own argument. Only then does
`--verdicts` produce a citation-accuracy figure, and only inside the checked-quote rung. The module judges
nothing itself: asking a model whether a quote supports a claim is the act that produced the claim.

**`claim_trace`** answers a different question about one record: *how did this get here?* Give it a claim ID and
it prints five things, each off a different artifact — the claim as written, with its epistemic status now and as
the run left it; the write events, and the phase derived from three independent sources with any disagreement
stated; every tool call the writing agent made, with arguments and outcomes; what the run's own `check_quote`
said about that quote, from both logs, each row naming the rule that linked it; and where the proposal cites
it, including which integrity-gate keys name it.

It exists because diagnosing one flagged claim by hand took five file reads and produced a wrong answer on the
way: a claim's `origin` field was read as if it named the writing phase. Three joins here are easy to invert and
the module states each in its output — the tool log is joined on `label`, not on its own `agent_id`, which is an
opaque hash; `origin` is not a phase; and a claim ID is not unique across knowledge bases, so a claim written by
another run is flagged before anything else is reported. An ID the store dropped as a duplicate names the ID it
kept instead of reporting nothing.

**It draws no conclusion about whether an agent read a quote or wrote it from memory, and never prints such a
label.** Tool inputs are truncated at 300 bytes and results at 200, agent transcripts are not archived, and an
agent's own transcript tree lies outside this log — so absence of a recorded call is not evidence of invention.
Each input reads as *absent*, *present and empty* or *present with no match*, never collapsed into "no calls".

**`quote_outcomes`** asks the same question of a whole run instead of one claim: what `check_quote` answered,
**per stage**, with the host's fetch attempts beside it. Per stage and not only per run because the literature
store grows *during* a run — a refusal at `literature` and the same refusal at `synthesize` are different
facts. It returns a structure (`--format json`), tabulates it by default, and is persisted into
`run-score.json` so `evaluation.figures` can draw it without reading a second file.

It keeps three logs apart, which is the mistake it was written after someone made by hand: `literature-calls.jsonl`
is the **host verifier** during the run, `literature-calls.agents.jsonl` is the **agents** during the run, and
`literature-calls.audit.jsonl` is `citation_audit`'s re-checks **after** it. The legs are never summed — they
are different callers — and the audit rows are in neither.

The agents' stage is **recorded**: the tool log carries it, and the refusal cause survives that log's 200-byte
truncation in every archived case. The host's log has no phase field at all, so its stage is **inferred from
timestamps** — the last stage that had started at or before the row — and every output says so. Read against
the fetch attempts, a refusal in a stage that attempted no fetch says the paper was never asked for, which is a
different fault from a paper the store could not get.

**`run_score`** reads every per-run measurement a pre-registration is judged against out of the archive —
cost, which phases ran, whether the hypothesis authors cited the evidence this run's own literature phase
produced, the blinding, the filesystem reach, the verifier's verdicts, the quote checks. A measurement whose
input is missing is reported as `null` with its reason, never as zero.

One of its rows is **output A measured as a shape**, which the integrity gate cannot give: `check_mechanism`
answers pass or fail, and a section citing nine edges that form one nine-step chain passes exactly like a
section citing two that happen to join. `mechanism_chain` states the length of the longest chain among the
causal claims section A **cites**, how many of those chain steps are `verified`, and it names the cited causal
claims lying **off** the chain — which is where a reader should look. It also counts `kb_mechanism_path` calls
per phase, so that a chain can be told from a coincidence. Computed over the cited edges only, from
`final-proposal.json` and `claims.jsonl`: what is scored is what the proposal says, not what the base could
have supported. The views the agents are given are the other side of this, in
[mechanism-views.md](../mechanism-views.md).
