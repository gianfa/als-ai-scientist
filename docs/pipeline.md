# The discovery pipeline

![The complete system: deterministic inputs and computation, the Python host that orchestrates the twelve
phases, the agents of each phase, the persistent knowledge graph the host alone writes, and the structured
proposal a run emits.](assets/imgs/als-system-cartoon.png)

A Python orchestrator runs one small Workflow per phase and is the only writer of a persistent, claim-based
knowledge base. It is what
`./run.sh` launches: the previous architecture's launcher was removed on 2026-10-03 and survives only in the git
history.

**Twelve phases**, in the order `pipeline/orchestrator.py::PHASES` declares them. `execute` and `reassess` were
added on 2026-10-05 and are documented as steps 9 and 10 below; they take no `phases.<name>` block in
`configs/pipeline.yaml`, so the configuration file lists ten keys while the pipeline runs twelve. The orchestrator's
`PHASES` tuple is the authoritative list.

Implemented phases, in order:

1. `data`: no agent. Every input is declared in `phases.data.sources`; `tools/data/
   local_sources.py` resolves, hashes and records them in `data-manifest.json`, giving each a citable
   `dataset:<id>@<hash>` reference. A declared required source that is missing stops the run, naming every absent
   source at once.
2. `compute`: no agent. The **four** analyses named in `phases.compute.analyses` (`a1_immunodominance`,
   `a2_epitope_clustering`, `a3_gwas_loci`, `a4_hla_binding`) run on the manifest's paths and write their verifiable artifact under
   `analysis/<id>/summary.json`; each verified artifact licenses knowledge-base claims, as "How a computed claim
   is made" below sets out. A failing analysis stops the phase, and an artifact from an earlier attempt is reused
   only if its digest verifies.
3. `literature`: one agent per question returns atomic claims with a source and a verbatim quote, checked with the
   `check_quote` tool before returning;
4. `generate`: independent authors each propose one hypothesis as causal and assumption claims, links to the
   evidence and declared ignorance;
5. `review`: at least two independent reviewers per hypothesis, blinded to author and confidence (as every
   phase is — `docs/experiment-selection.md`), bring quoted evidence (`supports` / `contradicts`) and gaps
   (`questions`, never negative evidence);
6. `verify`: an independent verifier judges, on the quoted passage alone, the agent-asserted `supports` /
   `contradicts` whose source claim is **checkable** — already verified, or carrying a quote even if unverified
   (widened on 2026-10-04: a quoted but unverified claim was never examined, and the citation audit found five
   overstatements of exactly that shape that nobody had ever judged). A claim with no quote is still not
   offered: there is nothing to compare it with; a confirmed relation is written again as a verified relation, and
   only then weighs on the target claim's epistemic status. The pool spans **every run** in the shared store,
   which is what buys cross-run verification and also what makes this the most expensive phase; it can be bounded
   to the current run's own relations with `phases.verify.only_on_run_kb` (default `false`). The verifier also judges whether the source claim
   says what its quote says **and no more** (`faithful`). A verdict that confirms the relation on the quote while
   finding the claim overstated used to be discarded whole; now the verifier is asked for the claim restated to
   its quote, and that restatement is written as a **new** verified claim, from which the relation is re-asserted,
   with a `derived_from` to the original, and the restatement goes through the same quote check as any other
   claim, with its own receipt. The overstated claim is left **exactly** as it was — in a live run that means
   still `verified`, because only a verified source claim is offered for verification at all, and `verified` has
   only ever meant that its quote was found in the paper, not that the claim is faithful to it. Nothing is
   corrected in place; instead the verifier's finding is recorded as an `Ignorance` questioning the overstated
   claim, the same demotion an unverified objection gets. A causal claim is never narrowed this way —
   its overstatement can sit in the cause, effect or context, which restating the prose would leave intact —
   and every refusal is named in the phase report (`not_narrowed`).

7. `design`: one designer per open hypothesis proposes 1-3 experiments (`EXP-` records, linked by `tests` to the
   claims they test) aimed at its unresolved or contested claims and declared gaps; each must have at least one
   outcome that would contradict a tested claim; cost and feasibility are qualitative (low / medium / high).
   Two fields are derived rather than refused when the designer leaves them wrong: `dataset_location` is read off
   the run's manifest (what the run holds is local, anything else would have to be acquired) when the designer
   left it empty, and an expected outcome about a claim missing from `tested_claims` adds that claim, since an
   outcome about a claim says the experiment bears on it. Both derivations are reported in the phase report under
   `converted_elements`. An explicit `local` on a dataset the run does not hold is still refused: that claims a
   provenance nobody can check.
   Each experiment declares its `kind`: `in_silico`, a computation over datasets it names in `dataset_refs` with a
   `dataset_location` (`local` for one this run declared, `remote` for one to acquire), or `wet_lab` for a trial,
   cohort or in-vivo study. A proposer may say what it would compute, never what a dataset contains, having seen
   only metadata; an experiment calling itself `local` while naming a dataset the manifest does not hold is
   skipped with that reason. The designer also declares markers and sample/clinical metadata needed by an
   `in_silico` test. A deterministic check after design compares these requirements with the run's manifest;
   its per-EXP verdict and the exact dataset hash are archived in `NN-design-feasibility.json` and passed to the
   arbiter. See "A dataset as the instrument of an experiment" below;
8. `arbitrate`: one independent arbiter per open hypothesis decides `buy` (which experiments), `rejected` (verified
   evidence contradicts an indispensable claim) or `stop`, with the value of information (`D-` records); a gap is
   a reason to test, not to reject. The data-feasibility verdict (below) informs this decision; it does not gate
   it — an arbiter may buy an experiment that is not yet `data_supported` (`docs/experiment-selection.md`);
9. `execute`: at most one agent, and only when the arbiter bought an experiment whose declared analysis has a
   **registered handler**. The agent's one tool takes an experiment identifier and nothing else: it cannot choose
   the analysis, the inputs or the parameters. The host re-hashes the declared inputs against the run manifest,
   runs the handler, verifies the receipt, and checks the agent's interpretation against a rule written before the
   result was known (`interpretation_check`). With no bought experiment, or none with a handler, the phase launches
   no agent and the run continues. Two handlers are registered (`docs/analyses.md`): `hla_panel_compact_v1`
   (closed live in a production run) and `ics_cytokine_skew_v1`, a re-analysis of the Michaelis ICS
   cytometry deposit, not yet exercised by a live run;
10. `reassess`: at most one agent, and only after `execute` produced a verified result. An independent judgement of
   what the result does to the hypothesis — `keep`, `weaken`, `reject` or `inconclusive` — bound by the host to this
   experiment, this `result_digest`, the tested claims and **at least one of the claims the computation wrote**: a
   reassessment that never cites the result it reassesses is refused, whatever its prose says. The refusal is real
   and has fired in production, where the prompt had not asked for that citation;
11. `intervene`: one intervener per hypothesis the arbiter did not reject proposes 0-2 candidate interventions
   (`INT-` records, linked by `depends_on` to the claims they rest on, so a candidate falls with them), each with
   modality, target, predicted effect, at least one off-target risk and one safety concern, patient stratification
   (output D) and biomarker (output E); no candidate, with a reason, is a valid answer. Candidates are research
   hypotheses, not treatment recommendations. A candidate may carry `drug_name`, a real compound's name given only
   when the agent is sure of it. If `phases.intervene.drug_lookup_enabled` is set (default `false`), the host then
   looks each name up in MyChem.info and commits a code-verified claim per name (found / no record / ambiguous)
   with a `depends_on` from the candidate; the lookup may make a live network call. The phase and its
   lookup can be exercised alone, without a full run, by `pipeline/intervene_drug_lookup_probe.py`
   ([Drug lookup](tools/drug-lookup.md)); no production run has used the lookup yet;
12. `synthesize`: one PI writes the final proposal (outputs A-H, at least five failure modes) from the KB only,
   citing `[[ID]]`s; it may conclude `insufficient_evidence`. A gate checks the citations against the KB and lists
   any `contradicted` / `contested` claim cited outside the failure modes, and any cited claim **this run's own
   verifier judged unfaithful** — `faithful: false`, that is "the claim says more than its quote". Both are
   warnings, and a third names any cited claim that **carries a quote and names no source** — unattributable by
   construction, since there is no paper to look in. None of the three fails the gate: on one audited run the
   second names 6 citations of which 3 were confirmed as overstatements by a human audit and 1 was a false
   alarm, so refusing on the flag would have blocked a citation a reader found sound; the third names exactly
   the three weaknesses that proposal did **not** disclose, because no tool of the run had reported them
   (`check_quote` is never called when a claim names no paper). The proposal and the gate are written to
   `final-proposal.json` and `final-proposal.md`.

   A citation tag holds exactly one ID (`[[C-1]] [[C-2]]`), which the prompt states. A tag naming several IDs is
   rewritten into one tag each before the gate reads it, and every rewrite is listed under `normalized_tags` in the
   phase report; a tag naming no valid ID is left as it is and fails the gate (`malformed_tags`), because a citation
   the gate cannot read is a citation it cannot check.

A phase with nothing to do (no open hypotheses, no relation to verify, no surviving hypothesis) is skipped without a launch.

## Run

A live run is paid (one `claude -p` per phase). From the repository root:

```sh
uv run python -m pipeline.cli --run-id pl-001                       # all implemented phases
uv run python -m pipeline.cli --run-id pl-001 --phases literature   # one phase
```

- Configuration: `configs/pipeline.yaml`; `configs/pipeline-smoke.yaml` is the smallest, cheapest variant (Haiku, 2 questions, 2 authors, 2.5 USD per phase, 5 USD per run), selected with `--config configs/pipeline-smoke.yaml` (objective, questions, perspectives, models, timeouts, `run_max_cost_usd`).
- Knowledge base: `knowledgebase/` at the repository root (git-ignored), shared by all runs.
- Run directory: `experiments/<run-id>/results/`: `context.json`, `state.json` (completed phases), per phase the
  injected workflow and its args (`workflows/`), the stream, `<NN>-<phase>-result.json` and
  `<NN>-<phase>-report.json` (written, seen again, rejected, invalid outputs, dropped elements, dropped agents, unresolved sources, launch cost and time). A faulty element of an agent output (a claim, a link) is dropped with its reason; only an output that cannot be read at all is invalid as a whole. `unresolved_sources` names, per agent, each claim this phase wrote whose source reference leads to no published identifier and no artifact of this system — a news page, a search-results string, a record id. The claim is written and the reference kept as the agent spelled it: the point is that the phase that wrote it says so, instead of the final gate saying it twelve phases later,
  `literature-calls.jsonl` (quote checks), and `transcripts/<stage>/` (the agents' transcripts, copied in because the harness
  deletes them; sizes and sha256 are in the stage result).
- Re-running with the same run ID resumes after the last completed phase. A resume must use the config the run
  started with; to resume with a changed config, pass `--config-change "<reason>"`: the change (old and new hash,
  reason, phase) is recorded in `state.json`, and each invocation writes its own `context*.json`.
- Progress: each phase prints one line on stderr when it starts (`… 3/12 literature running`) and one when it ends
  (`✓ … done in 4m12s`, or `✗ … stopped after …` before the error). A phase already completed on a resume prints
  `✓ … (already completed)`. Within a phase there is no finer progress.
- Quotes are checked against the literature store (`literature/store`, see [Literature tools](tools/literature.md)); cited papers it
  lacks are fetched from Europe PMC first, up to `fetch_max_new` per phase.

## Budgets and limits

Every limit below is set in `configs/pipeline.yaml` (or the smoke variant) and recorded with the run. When a limit
is reached the run says so; nothing is cut silently.

| Limit | What it caps | How it is enforced | When reached |
|---|---|---|---|
| `run_max_cost_usd` (per session) | what is left of the run's cap, handed to each `claude -p` session (**workflow agents included**) | CLI `--max-budget-usd`, set to `run_max_cost_usd` minus the `cost_usd` of the run's earlier stages | the CLI stops the session and its background agents ("Budget limit reached"); the phase fails, the run stops at that phase and can be resumed. A phase with less than 0.5 USD left is not launched. There is no separate per-phase budget: one below the run cap killed an early run at phase 5 with 3.04 USD spent and nothing written |
| `run_max_cost_usd` | estimated spend of the whole run, all phases | `pipeline/cost_watch.py` next to each launch | the launcher is stopped, `logs/cost-cap-hit.json` is written, the phase fails. The estimate has read 16–40% below the CLI's figure, so treat it as a backstop, not as the budget |
| a workflow that does not return | — | `pipeline/launcher.py::salvage_outputs` reads the stage journal | the agents that had finished are kept (`salvaged` in the launch record, with `salvaged_agents` and `lost_agents`); the others are reported as dropped. With nothing finished, the phase fails |
| `launcher.timeout_s` | wall time of one phase | `timeout` around `claude -p` and the CLI's background wait ceiling | the phase fails |
| `phases.<phase>.web.searches` | web searches per agent | stated in the prompt; enforced per session by the CLI's built-in cap `CLAUDE_CODE_MAX_WEB_SEARCHES_PER_SESSION`, set to the per-agent budget × the phase's agents | further searches return a notice telling the agent to continue with what it has; the prompt tells it to answer with what it has and state what it could not check |
| `phases.<phase>.web.fetches` | page fetches per agent | stated in the prompt only (no built-in cap for WebFetch) | soft: relies on the agent |
| web budget 0 | phases without web (`generate`, `verify`) | the web tools are moved to `--disallowedTools` for that session | the tool is not available |
| `fetch_max_new` | papers newly downloaded into the literature store per phase | `pipeline/provenance.py` (Europe PMC open access) | further cited papers are not fetched; their quotes stay unverified; listed under `fetched` in the phase report |
| `phases.literature.max_claims` | claims per literature agent | prompt | soft |
| `phases.review.reviewers` | independent reviewers per hypothesis (≥ 2) | workflow | — |
| `phases.verify.batch_size` | relations judged by one verifier | orchestrator | more batches, one verifier each |
| `phases.verify.only_on_run_kb` | **which** relations are offered: the whole store (`false`, the default and the recommendation) or only the ones this run wrote (`true`) | orchestrator | with `true` the backlog of earlier runs is never judged and never narrowed; cheaper, strictly less verification, and the phase's counts stop being comparable with runs that left it `false` |
| web in `design`, `arbitrate`, `intervene`, `synthesize` | none | web tools disallowed in those sessions | the agents work from the KB slice only |

Notes:

- The per-session spend cap binding the workflow's agents was observed with CLI 2.1.287: it stopped
  the review phase at 1.01 USD of 1 USD. Older CLI versions applied the cap to the launcher session only.
- The built-in search cap and its "continue with what you have" behaviour come from the Claude Code tools reference
  (session search limit, CLI ≥ 2.1.212); it has not yet been observed in one of our runs.
- A phase with nothing to do is skipped without a launch, and its report says why.

## What a record's status means

`provenance_status: verified` means the claim's quote was found exactly in the stored paper, nothing more. The
epistemic status (supported, contradicted, contested, unresolved) is never stored: it is computed from verified
relations (`knowledge_base/status.py`). Relations proposed by agents are unverified inferences and do not weigh on
any status until the `verify` phase confirms them; a verified relation still rests on an LLM's reading of a
verbatim passage.

## How a source reference is recorded

An agent writes a source however it likes. Across the shared knowledge base the same kind of reference
appears in three forms — 650 bare DOIs, 203 PMCIDs, 136 with a `pmid:` prefix — and 110 references over 43
distinct strings name no identifier at all: a bare number, a claim id (`C-111`), a search phrase (`Web
search results`), a news domain. Nothing joined them, so "what did this run read" had no answer.

**The agent must declare what kind of identifier it is giving.** `ProposedClaim.source` is refused unless it
is `doi:10.xxxx/...`, `PMID:12345678`, `PMCID:PMC1234567`, `arxiv:…`, another declared scheme, or a full
`https://` URL. This is a validator on the structured output, not a sentence in a prompt: the agent cannot be
relied on to comply and the contract can. The point is **diagnosability** rather than purity — a typed
reference can be chased later, by hand or by a search, while a bare `41034581` or the sentence `Web search
results` cannot, and both are real values from production runs. Declaring the kind also retires the one guess
the canonicaliser made: an untyped five-to-nine-digit number used to be read as a PMID and is now refused.

**Having no source is legal; pretending to have one is not.** A claim with an empty source is recorded with
`origin: agent` — the system's own reasoning, not literature. That escape hatch is deliberate and must stay
open: a schema that demands an identifier without one creates pressure to invent a well-formed DOI, and
fabrication is the failure this project is built against. Behind the validator the host does the same thing
defensively: a source that arrives undeclared demotes the claim to `origin: agent`, and the string the agent
wrote is kept in `source_refs_raw` rather than discarded.

**The host canonicalises, the agent is not asked to.** It never writes to the knowledge base, so compliance
is not something to request in a prompt: `pipeline/proposals.py` passes every reference through
`tools.literature.store.canonical_id` — the one canonicaliser, the same the store's ingest path uses — as it
builds the record. `doi:<lower>`, `pmid:<digits>`, `pmcid:PMC<digits>`; a receipt, a dataset version, an
experiment or a result is an internal reference and is left untouched.

Three rules govern the rest:

- **What cannot be resolved is kept exactly as written**, never dropped and never rewritten. A claim resting
  on nothing checkable must stay visible, and inventing an identifier for it would be worse than the
  problem. The proposal gate names it as `cites_an_unresolvable_source`, with the string itself so a reader
  can judge it — a warning, like the others, never a rejection.
- **The form the agent wrote is kept beside the canonical one** (`source_refs_raw`), because provenance is
  what was said, not what we made of it. It is empty when nothing had to change.
- **Records written before 2026-10-06 are not rewritten.** The store is append-only and has no retraction,
  so the earlier references stay as they are; `evaluation/sources.py` resolves them at read time through the
  store's alias map, and reports what it cannot.

One limit, stated rather than hidden: a bare number of five to nine digits is read as a PMID. In a
`source_refs` list it could be something else. The alternative was a second, stricter canonicaliser used
only here, which would have disagreed with the one the ingest path uses — one rule with its limit named
beats two rules each right somewhere.

## Who decides what, and what a refusal means

Three different parties can stop a hypothesis, and they are not interchangeable. Reading a run's outcome without
keeping them apart is the easiest way to over-read it.

- **The arbiter** (`arbitrate`, one per hypothesis) can `reject` — but only on *verified* evidence contradicting
  an indispensable claim. A gap is a reason to test, not to reject.
- **The PI** (`synthesize`, one per run) writes the proposal and may conclude `insufficient_evidence` instead of
  naming a direction. This is a judgement over the whole board, and it is **downstream of the arbiter**.
- **The host** refuses structurally: an unknown identifier, a malformed citation tag, an experiment with no
  contradicting outcome, a reassessment that does not cite the result it reassesses. These are mechanical and
  say nothing about the science.

Two consequences, both observed in a production run, which should be stated whenever an outcome is reported:

1. **A declining run is not the same as a selection having happened.** In that run the arbiter rejected
   **none** of the three hypotheses; the PI declined downstream, because no core mechanistic claim had been
   directly tested. That outcome is a **suspension of judgement**, not a hypothesis having been eliminated. A
   reader who sees `insufficient_evidence` and infers that the system ruled something out has read it backwards.
2. **Refusing is the cheap outcome.** A system that always deferred would produce an artifact of the same shape,
   with the same gate passing, at the same cost. Two of the three production runs declined. Until a run is
   observed *committing* when the evidence is good, this project cannot claim the system **discriminates**
   rather than being merely prudent — and no number in the archive settles it, because the archive records what
   the system did, not what it should have done.

Neither point is an argument for weakening the gates. A valid run may conclude that the evidence is insufficient,
and the rules exist to protect that conclusion. The point is that the conclusion must be reported for what it is.

## What a run inherits from the ones before it

The knowledge base is **persistent and shared across runs**, so a run is not a fresh start. This is the
system's only form of iterative improvement, and it is worth stating plainly because it has effects in three
directions.

**What accumulates.** Every claim, link, hypothesis, experiment, decision and intervention a run writes stays,
with its `run_id` and its provenance. A later run's slice is drawn from the whole store, so it meets what earlier
runs established: `literature` can find a paper already read, `generate` can build on a claim written months
before, and `verify` judges relations whose source claim may belong to any run. A claim already recorded is not
written twice — the canonical hash recognises it and the host records a `seen_again` event, so **a repetition is
logged as a repetition and never as fresh corroboration**.

**What improves, and what does not.** What accumulates is *evidence*, not *skill*: nothing is learned, no weights
are fitted, no prompt is updated by a run. The system gets better across runs only in the sense that the pool it
reasons over is larger and more of it has been checked. Verified claims rose from 27 to 45 between two runs while
the number of quoted claims stayed at 42, which is the shape this improvement takes: the same work, more of it
surviving verification.

**Claims persist, hypotheses do not.** The durable layer is the claims: their status is recomputed as new
evidence arrives, across runs. Hypotheses are built on claims and belong to the run that generated them; a later run
does not revise them but generates its own, on claims whose status may by then have changed. This is by design, and
it is why there is no hypothesis-revision step.

**Three consequences a reader must keep in view.**

1. **A contradiction usually lands on an *earlier* run's hypothesis.** Verification draws on the whole store, so
   the claims a run contradicts are typically not its own. This is why hypothesis revision inside a run was
   measured to be nearly unreachable and then deliberately not built: the information that would
   revise a hypothesis arrives after its run has ended.
2. **Per-run measurements are confounded by the shared pool.** "Fresh claims cited" is counted against a
   denominator that grows between runs, so the three production runs are not independent replicates. Any figure
   about a run's own evidence reach must be read with that in mind.
3. **An error propagates.** The cost of persistence is this: a wrong claim admitted once is
   available to every later run. The store is append-only and has nothing resembling retraction; the mitigations
   are the `run_id` and provenance on every record, the computed epistemic status (a claim later contradicted
   becomes `contradicted` without being edited), and the gate that reports a `contradicted` or `contested` claim
   cited outside the failure modes.

**This is a limitation we accept, not one we failed to notice.** The design decision was taken with its cost written down:
cross-run persistence risks carrying an error into later runs, and the mitigations are provenance on every record
and a status that is computed rather than written. The alternative — a store that can retract — was rejected
because a retraction is an unrecorded edit: it makes the knowledge base disagree with the archives of the runs
that cited it, and it destroys the one property every claim in the technical report rests on, that any statement
can be traced back to the record as it stood when it was used. We would rather carry a wrong claim that is
visibly contradicted than hold a clean store nobody can audit.

**How to turn the accumulation off.** It is a path, not a mode: `--kb <dir>` selects the knowledge base a run
uses, defaulting to `knowledgebase/` at the repository root. Point it at an empty directory and the run starts
from nothing, inherits nothing and leaves the shared store untouched.
There is no configuration key and no named "isolated run" switch; the flag is the whole mechanism. Note what an
isolated run costs: no prior graph, no earlier verification, no `seen_again`, so every claim it meets is one it
paid to write itself.

## Two causal graphs, and why they are not the same object

The knowledge base holds causal claims, each an edge from a cause to an effect with a context and a mechanism;
the entity names say which way the effect goes (`Decreased C9orf72 expression`), and there is no separate sign
(ADR 0032). A claim is causal **because it carries that block**, not because of a label: its `type`
(`observational`, `quantitative`, `methodological`, `assumption`) says what kind of statement it is, and the two
are independent, so a mechanism observed in patients is an `observational` claim with the block filled. Two things are built from them, and confusing them is easy:

- **The knowledge-base graph** (`knowledge_base/status.py::mechanism_graph`, served to the agents as
  `mcp__kb__kb_mechanism_graph`) mirrors every current causal claim of every run, each carrying the status
  computed from verified relations. It is a view: it grows while a run proceeds, it is recomputed on every call,
  and it constrains nothing. A snapshot of what a run inherited is written once at its start
  (`inherited-mechanism-graph.json`), for provenance.
- **The prior causal graph** (`pipeline/prior_graph.py`, `prior-causal-graph.json`) is a *selection*: which of
  those edges, read from the sources before any hypothesis exists, form the scaffold a hypothesis is expected to
  attach to. It is packed into the hypothesis authors' prompts, so an author reuses the entities already there
  and aims at the places where the structure is weak.

The division follows what each thing is. What a source states is **evidence**: it belongs in the append-only
knowledge base, where a claim can be superseded but never removed. Which edges constitute the prior is a
**modelling choice** we may revise, so it lives in a versioned artifact that references claim IDs and owns no
content: changing the prior is a new version of a file (`prior:v1@<digest>`, recorded in the literature phase
report), not a retraction in a store that has no retraction.

Selection rule v1 is mechanical and stated in the artifact: the causal claims of this run that carry a source and
whose quote was verified against it, taken at the end of the literature phase. A claim does not record which
agent wrote it, so the moment of construction is part of the rule; a resumed run reuses the file rather than
reselecting over a knowledge base the later phases have grown. Neither graph is a causal model identified from
data, and both say so where an agent can read it.

### Drawing the graph

`tools/graph/` builds the whole knowledge-base graph — every record kind as a node, every relation as an edge,
plus the links a relation does not carry (a claim to its entities, an experiment to its hypothesis, a decision to
what it buys) — and renders it:

```sh
uv run python -m tools.graph --kb knowledgebase --run <RUN_ID> --format dot --out run.dot
uv run python -m tools.graph --kb knowledgebase --focus H-14 --hops 2 --format mermaid
```

Five formats, each in its own module so one cannot break another: `json`, `dot` (needs Graphviz), `mermaid`
(renders in a Markdown viewer, which is the path for a figure in the report) and `svg`/`html`, which are
**self-contained and need nothing installed** — no stylesheet, script or font is fetched, so the file opens from
disk in any browser and in an archive years later. The HTML page states how many nodes and edges it holds, which
filters produced it, how many edges are not drawn and why, and that it is not a causal model identified from data;
hovering a node or an edge gives the full text a label had to cut. The layout is a deterministic ellipse ordered
by kind then ID, so a figure does not move between runs; it is readable for a focused neighbourhood and
unreadable past about forty nodes, which is a property of circular layouts and not a bug to work around silently.

It is read-only, never overwrites an output file, and fails on a path that is not a knowledge base rather than
printing an empty graph. Colour carries the epistemic status of a claim, a dashed arrow an unverified relation, a
dashed node a superseded record. `--run` and `--focus` narrow the picture, and the result reports `dropped_edges`
with the reason, because an edge whose other end was filtered out would otherwise vanish silently where a reader
is looking for structure.

`tools.graph` is the one graph builder for the pipeline, and `knowledge_base/status.py::mechanism_graph` stays the
causal view the agents read.

### Reading a mechanism out of the graph

`mechanism_graph` returns a *set* of causal edges, and a set that happens to be disconnected is a list
of causes rather than a pathway — which is what a mechanism gate reported on one run while its own
knowledge base held a six-edge chain. Three further views in the same module close that gap:
`causal_edges` filters the edges by provenance and by computed epistemic status, `mechanism_path`
returns the longest directed chain as an ordered list, and `mechanism_subgraph` returns that chain plus
the one-hop neighbourhood of each of its nodes — the view to hand a reviewer, because the backbone
carries each step's provenance and its neighbours show what competes with it. The longest path is one
selection rule and not the only defensible one. All of it, with the measurements and the alternatives:
[mechanism-views.md](mechanism-views.md).

## A dataset as the instrument of an experiment

A dataset the system knows of but has not analysed is recorded neither as a claim nor as a gap: it is the
instrument of a proposed experiment, weighed by the arbiter like any other.
Two distinctions, orthogonal:

| | **dataset local** (declared, hashed) | **dataset remote** (to acquire) |
|---|---|---|
| **`in_silico`** (computation) | data may support the test; code and sample metadata still need checking | requires acquisition and a feasibility check |
| **`wet_lab`** (trial, cohort, in vivo) | unusual: a study whose readout reuses local data | a proposal only: nothing here can execute it |

The data-feasibility verdict is `data_supported`, `missing_inputs`, or `unverified`. It uses the declared
requirements and the run's manifest, with exact marker names; a marker absent from the recorded FCS panel is a
missing input, while a clinical variable with no inventory remains unverified. `required_metadata` works the
same way, against the exact keys a cytometry source's `metadata_fields` inventory reports — one production run
measured the cost of getting this wrong: its designers wrote free prose ("donor disease-group label") instead
of a reported key, which a strict inventory match can never resolve, so every cytokine-skew EXP it proposed
came back unverified and none was bought. The design prompt now says to use the inventory's own keys, as it
already did for markers. An empty requirement declaration is unverified, never a pass. The verdict, requirements,
source reference/hash and reason appear in
`NN-design-feasibility.json`, in the arbiter's archived arguments, and beside cited experiments in the final
proposal. `data_supported` means the named measurements exist; it says nothing by itself about executable analysis
code. Two registered handlers are the exception (`docs/analyses.md`): `hla_panel_compact_v1` (three exact local
sources) and `ics_cytokine_skew_v1` (two exact local sources, the Michaelis cytometry deposit and its source
data). Each checks its own exact sources and, after the arbiter buys a matching EXP, the `execute` phase lets a
computational agent call a tool that takes only that EXP ID; `analysis_id` in the verified artifact binds which
handler's preregistered outcome-to-interpretation map the agent's interpretation is checked against. The host
verifies the resulting artifact and a separate agent reassesses the hypothesis. The final proposal identifies a
performed EXP only when a verified execution receipt exists. Other bought experiments remain proposals. No live
run has yet demonstrated an agent proposing and buying `ics_cytokine_skew_v1`; remote dataset acquisition
remains separate work.

## How a computed claim is made

`compute` and the registered post-arbitration `execute` phase can produce evidence that is not read from a paper.
Their claims are fixed by the verified artifacts and the rules in code (`pipeline/compute.py`).

- **One `quantitative` claim per numeric result**, with its content templated from the artifact's own `results`
  entry (`key`, `value`, `n`, `test`). It is terse on purpose: interpreting a number is the work of an agent that
  cites the claim, not of this code.
- **One `observational` claim carrying the conclusion written in the analysis script**, prefixed `Negative result`
  when the analysis sets `negative_result`. A negative result is evidence and is not dropped.
- **Every computed claim is `origin: computation`, `verification_method: code`, `provenance_status: verified`**,
  which here means that re-running the analysis on the inputs named in `source_refs` reproduces it. The references
  name both the dataset versions (`dataset:<id>@<hash>`) and the computation (`result:<analysis_id>@<digest>`), so
  a changed input, script or seed yields a different claim rather than the same one.
- **No relation is written.** Linking a number to a hypothesis is a judgement, left to the agents that can cite
  the claim and whose inference the `verify` phase can then weigh.

**Whether anyone uses it is a separate question, and it is measured.** In one smoke run eleven computed claims
entered the knowledge base and not one was cited, linked or tested. Counting why showed the cause was not
indifference: of 309 current claims, the hypothesis authors' slice held 40, all `origin: literature`, and none of
the computed ones — literature accumulates across runs while computed claims are few by construction, so a
verified-first sort buried them. Two changes followed: the slice reserves up to a quarter of its places for
computed claims (`Orchestrator.COMPUTED_SHARE`), and six phase prompts now say what `origin: computation` with
`verification_method: code` means and that such a claim is cited by ID like any other. The proposal gate reports
`computed_claims_cited`, so the next run answers the question by itself instead of being re-measured by hand.

The computation reference deliberately ignores which run produced the artifact: its digest is taken over the
artifact minus `provenance`, `result_digest` and `git_commit`. Without that, the same computation cited a different
reference in every run, because the envelope carries the run id. The artifact's own `result_digest` is what
`analysis/verify.py` checks; the reference is a citation, not a replacement for it.

Three claims of one run, as written (`probe-compute-001`):

```
C-2  quantitative   a1_immunodominance: rank_one_probability = 0.3456; n = 10000; binomial bootstrap.
C-3  observational  Negative result of a1_immunodominance: Table 1 alone does not identify a single
                    immunodominant C9orf72 epitope; ...
C-7  quantitative   a3_gwas_loci: lambda_gc = 1.1171; n = 10461755; median chi-square.
```

## How an agent reaches the knowledge base

Each phase receives a slice of the KB in its prompt, packed by the orchestrator (`kb_context` for `generate`, the
hypothesis and its *k*-hop neighbourhood for the later phases). The slice is bounded by `context_limit` and `hops`,
so it is not the whole KB, which is shared across runs.

Beyond the slice, every phase session is granted five read-only tools (`pipeline/launcher.py`, `READ_TOOLS`),
served by `knowledge_base/mcp_server.py`: `mcp__kb__kb_get` (records by ID), `mcp__kb__kb_neighbourhood` (a record
and its links), `mcp__kb__kb_search` (keyword; it searches claims and entities by default, and reaches the
system's own records — hypotheses, declared gaps, experiments, decisions, interventions — only when the call
passes `types`), `mcp__kb__kb_mechanism_graph` (the causal edges as a set) and `mcp__kb__kb_mechanism_path` (the
longest connected chain through them, plus the unverified edges adjacent to it — see
[mechanism-views.md](mechanism-views.md)). Seven of the eight phases name them in their prompts, so an agent
can resolve an ID it meets, check whether a claim is already recorded, and build on earlier runs. `verify` does not
name them on purpose: its verdict must rest on the quote it is handed. The tools cost no web budget, and writing
stays with the orchestrator — `Bash`, `Edit` and `Write` are in `--disallowedTools` for every phase.

Those MCP servers are the only sanctioned way for an agent to reach data, so the session's built-in tools are
restricted to an exclusive set with `--tools` (`BUILTIN_TOOLS` in `pipeline/launcher.py`): `Workflow`, `ToolSearch`
(the agents load the MCP schemas through it), `Read` (the `Workflow` tool refuses a scriptPath it cannot read) and
the web tools of phases that have a web budget. `--allowedTools` alone was not enough: it only pre-approves, and
with `--permission-mode dontAsk` every other built-in stayed callable — a reviewer in one smoke run called `Grep`
on the repository.

## How an agent reaches the data

The `data` phase writes `data-manifest.json`; `tools/data/mcp_server.py` then serves it read-only to the agents as
`mcp__data__data_list` (every dataset: id, kind, size, licence note, and the ones declared but not found) and
`mcp__data__data_describe` (one dataset by id, with the analyses configured to read it, and its **shape** where the files state one: for a flow-cytometry deposit, the channels recorded and the markers its FlowJo workspaces gate on. A marker absent from that list was not measured, so an experiment needing it cannot be run on that data — one production run bought two experiments resting on IL-5 when the manifest said only "141 raw files, 2 GB"). Every phase but `verify`
names them in its prompt, for the same reason it names the KB tools; the launcher registers the server only once
the manifest exists.

The server returns **metadata only, and no path**: reading a data file is the job of code that writes a verifiable
artifact, so a path would invite a read that cannot be verified. The prompts say so, and say where
computed facts come from instead: the claims of the `compute` phase, which an agent may cite. `analysed_by` makes
an unanalysed dataset visible — in `probe-compute-001` the 2.0 GB Zenodo deposit answers `[]`, which is what a
hypothesis author needs to know.

## How an agent reaches the papers

Agents have no shell, so the paper library is served to them by a dedicated MCP server, `tools/literature/mcp_server.py`.
`mcp__literature__check_quote(paper, quote)` looks a quotation up in the local literature store (`literature/store`) and
says whether the text is there; it never reads a PDF or the network itself, only texts already stored. In a phase
that has a fetch budget (`phases.<phase>.agent_fetch_max_new`), `mcp__literature__fetch_paper(identifier)` also brings
in an open-access paper by DOI, PMID or PMCID, so that its quotations can be checked straight away. The tools, the
verdicts and the budgets are described in [Literature tools](tools/literature.md#the-mcp-server-mcp_serverpy).

## Did the agents use a tool? (one cheap run)

`<stage>-agent-tools.jsonl` in a run's results holds one row per tool call made by an agent. `evaluation/tool_usage.py`
reads it:

```sh
uv run python -m evaluation.tool_usage experiments/<RUN_ID>/results \
    --expect mcp__data__ --format markdown
```

It prints the calls per tool, per stage and per agent, and exits 1 when no agent called a tool whose name starts
with `--expect`, so the answer cannot be read optimistically.

`--diagnose` adds three plain signals, with no interpretation: a call **repeated** identically by the same agent, a
call the runtime answered with an **error**, and a call answered with **nothing** (`{"records":[]}` from a search of
an empty knowledge base counts, which size alone could not tell). Each archived row carries its outcome
(`status`, `result_bytes`, `result_preview`), so a diagnosis no longer depends on the runtime's own transcript,
which lives outside the archive and may be deleted. Rows from an older run have no outcome and are counted as such
rather than judged. On `probe-tools-20261003-112705` the report reads:

```
- repeated: author-2 called `ToolSearch` 4x with the same input
- repeated: author-2 called `ToolSearch` 3x with the same input
- repeated: author-2 called `ToolSearch` 2x with the same input
- empty: author-1 / `mcp__kb__kb_search` (02-generate) returned nothing
```

To answer the question before committing to a full run, run the free `data` phase and **one** agent phase
(`generate`: roughly 0.2 USD and two minutes, against ~3 USD and 17 minutes for a full twelve-phase run) into a
throwaway knowledge base inside the run directory, then apply the check above:

```sh
mkdir -p experiments/<RUN_ID>/kb
uv run python -m pipeline.cli --run-id <RUN_ID> --config configs/pipeline-smoke.yaml \
    --phases data,generate --kb experiments/<RUN_ID>/kb
```

It is a real run of the real prompt: nothing in it tells the agents to use the tool, which is the point. It exists because a
granted, prompt-named tool has twice turned out to be unused — three phases ignored the KB tools in
one smoke run.

## Inspect the knowledge base

```sh
uv run python -m evaluation.kb_stats --format markdown   # records, provenance, verification yield, cost per run
uv run python -m knowledge_base.mcp_server --kb knowledgebase --check
uv run python -c "from knowledge_base.store import read_records; from pathlib import Path; print(len(read_records(Path('knowledgebase'))))"
```

## Tests

`uv run pytest tests/test_knowledge_base_*.py tests/test_pipeline_*.py` and
`node --test tests/workflow/phases.test.js` (mocked agents, fake launcher, no paid calls).
