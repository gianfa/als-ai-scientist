// Phase workflows of the pipeline, run on their built files with mocked agents.

import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'

import { build } from '../../workflows/build_workflow.mjs'
import { runWorkflow } from './harness.js'

const path = name => fileURLToPath(new URL(`../../workflows/phases/${name}.js`, import.meta.url))
const src = name => fileURLToPath(new URL(`../../workflows/phases/src/${name}.js`, import.meta.url))
const SCHEMA = { type: 'object', properties: { claims: { type: 'array' } }, required: ['claims'] }

const literatureArgs = (over = {}) => ({
  phase: 'literature', run_id: 'RUN-1', objective: 'C9orf72 autoimmunity in ALS', schema: SCHEMA,
  questions: ['Which T cells respond to C9orf72?', 'Which cytokines?'], kb_context: [], model: 'claude-haiku-4-5',
  web: { searches: 6, fetches: 6 }, ...over,
})

for (const name of ['literature', 'generate', 'review', 'verify', 'design', 'arbitrate', 'execute', 'reassess', 'intervene', 'synthesize', 'novelty']) {
  test(`built ${name} phase is in sync with its source`, () => {
    assert.equal(readFileSync(path(name), 'utf8'), build(src(name)))
  })
}

test('execute gives one agent only the selected EXP and the narrow tool', async () => {
  const { calls } = await runWorkflow(() => ({}), {
    args: { phase: 'execute', run_id: 'R', objective: 'o', schema: SCHEMA,
            selected: [{ experiment_id: 'EXP-9', analysis_id: 'hla_panel_compact_v1' }] },
    path: path('execute'),
  })
  assert.deepEqual(calls.map(c => c.label), ['executor@EXP-9'])
  assert.ok(calls[0].prompt.includes('mcp__experiment__run_experiment(experiment_id)'))
  assert.ok(calls[0].prompt.includes('cannot establish population HLA coverage'))
})

// The defect a real run exposed: four bought eligible EXPs, one executed, and three dropped with no record.
// The phase must launch one agent per EXP, each told only its own, so a second registered handler
// can run in the same run as the first.
test('execute launches one agent per bought EXP, each told only its own', async () => {
  const { calls } = await runWorkflow(() => ({}), {
    args: { phase: 'execute', run_id: 'R', objective: 'o', schema: SCHEMA,
            selected: [{ experiment_id: 'EXP-1', analysis_id: 'hla_panel_compact_v1' },
                       { experiment_id: 'EXP-2', analysis_id: 'ics_cytokine_skew_v1' }] },
    path: path('execute'),
  })
  assert.deepEqual(calls.map(c => c.label), ['executor@EXP-1', 'executor@EXP-2'])
  // Each prompt carries its own analysis block and its own EXP, and never the other agent's.
  assert.ok(calls[0].prompt.includes('cannot establish population HLA coverage'))
  assert.ok(!calls[0].prompt.includes('EXP-2'))
  assert.ok(calls[1].prompt.includes('exact two-sided Mann-Whitney test'))
  assert.ok(!calls[1].prompt.includes('EXP-1'))
  assert.ok(calls[1].prompt.includes('EXP-2 is yours and the only one you may touch'))
})

test('execute refuses an empty or non-array selection rather than running nothing quietly', async () => {
  for (const selected of [[], { experiment_id: 'EXP-9' }]) {
    await assert.rejects(runWorkflow(() => ({}), {
      args: { phase: 'execute', run_id: 'R', objective: 'o', schema: SCHEMA, selected },
      path: path('execute'),
    }), /non-empty array/)
  }
})

test('reassess gives a separate critic the executed result and hypothesis', async () => {
  const { calls } = await runWorkflow(() => ({}), {
    args: { phase: 'reassess', run_id: 'R', objective: 'o', schema: SCHEMA,
            executions: [{ execution: { status: 'executed', experiment_id: 'EXP-9', hypothesis_id: 'H-2' },
                           hypothesis: { hypothesis_id: 'H-2' } }] },
    path: path('reassess'),
  })
  assert.deepEqual(calls.map(c => c.label), ['reassessor@EXP-9'])
  assert.ok(calls[0].prompt.includes('independent post-result critic'))
  assert.ok(calls[0].prompt.includes('inconclusive rather than force'))
})

// The host guard in `_reassess_phase` demands that claim_ids hold one of the computed-result claims. If the
// prompt does not say so, the agent cites the tested claim alone and a correct judgement is thrown away. The
// prompt must state every binding the host enforces.
test('reassess tells the critic to cite a computed-result claim, as the host guard requires', async () => {
  const { calls } = await runWorkflow(() => ({}), {
    args: { phase: 'reassess', run_id: 'R', objective: 'o', schema: SCHEMA,
            executions: [{ execution: { status: 'executed', experiment_id: 'EXP-9', hypothesis_id: 'H-2',
                                        tested_claims: ['C-2'], written: ['C-1'] },
                           hypothesis: { hypothesis_id: 'H-2', computed_result_claims: [{ ref: 'C-1' }],
                                         citable_context_claims: ['C-7', 'C-8'] } }] },
    path: path('reassess'),
  })
  assert.ok(calls[0].prompt.includes('computed_result_claims'))
  assert.ok(calls[0].prompt.includes('at least one is required'))
  assert.ok(calls[0].prompt.includes('not\n  sufficient on their own'))
  // The citable set is named, not left to be inferred from the slice: one reassessor cited a
  // verified claim of the slice it was shown and a guard built on the tested claims alone refused it.
  assert.ok(calls[0].prompt.includes('["C-7","C-8"]'))
  assert.ok(calls[0].prompt.includes('["C-2"]'))
})

// A run was refused on the other binding the prompt never stated: its reassessor wrote the digest
// with a parenthesis appended, and the host compares it character for character.
test('reassess tells the critic that result_digest is the bare digest', async () => {
  const { calls } = await runWorkflow(() => ({}), {
    args: { phase: 'reassess', run_id: 'R', objective: 'o', schema: SCHEMA,
            executions: [{ execution: { status: 'executed', experiment_id: 'EXP-9', hypothesis_id: 'H-2',
                                        result_digest: 'a'.repeat(64) },
                           hypothesis: { hypothesis_id: 'H-2' } }] },
    path: path('reassess'),
  })
  assert.ok(calls[0].prompt.includes('copy the execution report\'s digest exactly'))
  assert.ok(calls[0].prompt.includes('no\ncommentary, outcome or parenthesis appended'))
  assert.ok(calls[0].prompt.includes('compared character for character'))
})

// Two EXPs of one run can test the same hypothesis (a real run: H-1 bought both EXP-1 and EXP-2), so the
// labels are keyed by EXP: `reassessor@H-1` twice would collide and lose a judgement.
test('reassess launches one critic per executed EXP, labelled by EXP and not by hypothesis', async () => {
  const entry = (eid, claim) => ({
    execution: { status: 'executed', experiment_id: eid, hypothesis_id: 'H-1', tested_claims: ['C-2'],
                 written: [claim], result_digest: 'a'.repeat(64) },
    hypothesis: { hypothesis_id: 'H-1', computed_result_claims: [{ ref: claim }],
                  citable_context_claims: ['C-7'] },
  })
  const { calls } = await runWorkflow(() => ({}), {
    args: { phase: 'reassess', run_id: 'R', objective: 'o', schema: SCHEMA,
            executions: [entry('EXP-1', 'C-1'), entry('EXP-2', 'C-3')] },
    path: path('reassess'),
  })
  assert.deepEqual(calls.map(c => c.label), ['reassessor@EXP-1', 'reassessor@EXP-2'])
  assert.ok(calls[0].prompt.includes('EXP-1') && !calls[0].prompt.includes('EXP-2'))
  assert.ok(calls[1].prompt.includes('EXP-2') && !calls[1].prompt.includes('EXP-1'))
  assert.ok(calls[0].prompt.includes('judge only this one'))
})

test('reassess refuses a report whose entries are not executed', async () => {
  await assert.rejects(runWorkflow(() => ({}), {
    args: { phase: 'reassess', run_id: 'R', objective: 'o', schema: SCHEMA,
            executions: [{ execution: { status: 'not_executed', experiment_id: 'EXP-9' },
                           hypothesis: { hypothesis_id: 'H-2' } }] },
    path: path('reassess'),
  }), /must be an executed result/)
})

// The dead end `fetch_paper` exists for: 192 of the agents' 760 archived `check_quote` calls were refused
// `paper_not_in_store` on a resolvable identifier, and the prompt used to tell them the host would fetch it
// later. The tool is described exactly when the config grants a budget, because that is also when
// the server registers it.
test('literature tells the agent to fetch a paper it identified, when it has a budget', async () => {
  const { calls } = await runWorkflow(() => ({}), {
    args: literatureArgs({ agent_fetch: 8 }), path: path('literature'),
  })
  const prompt = calls[0].prompt
  assert.ok(prompt.includes('mcp__literature__fetch_paper'))
  assert.ok(prompt.includes('At most 8 papers in this phase'))
  assert.ok(prompt.includes('**once** with that identifier'))
  // The losing instruction is gone: nothing tells the agent that someone else will fetch it later.
  assert.ok(!prompt.includes('it will be checked later'))
  // A paywall is to be reported, not hidden and not quoted from memory.
  assert.ok(prompt.includes('say so in the claim, with the identifier'))
  // But `failed` must not be lumped with it. `tools/literature/fetch.py` defines `failed` as "a failure to
  // retry, not evidence that the paper has no open copy", and the service's own rate limit surfaces as
  // exactly that — shared across every agent of the phase, since the limit is per IP. Telling the agent to
  // write it into a claim would record a throttled request as a fact about access.
  assert.ok(prompt.includes('**`failed` is different**'))
  assert.ok(prompt.includes('not that the paper is unavailable'))
})

test('a phase with no fetch budget says nothing about the tool', async () => {
  for (const args of [literatureArgs(), literatureArgs({ agent_fetch: 0 })]) {
    const { calls } = await runWorkflow(() => ({}), { args, path: path('literature') })
    assert.ok(!calls[0].prompt.includes('fetch_paper'))
    assert.ok(calls[0].prompt.includes('mcp__literature__check_quote'))
  }
})

test('review gets the same fetch instruction, and verify never does', async () => {
  const review = await runWorkflow(() => ({}), {
    args: { phase: 'review', run_id: 'R', objective: 'o', schema: SCHEMA, reviewers: 2, agent_fetch: 4,
            items: [{ hypothesis_id: 'H-1', context: [{ id: 'C-3' }] }] },
    path: path('review'),
  })
  assert.ok(review.calls[0].prompt.includes('mcp__literature__fetch_paper'))
  assert.ok(review.calls[0].prompt.includes('At most 4 papers in this phase'))
  // `verify`'s verdict must rest on the quote it is handed, so it names no tool at all — a budget it is never
  // given could not change that, and the phase does not read the field.
  const verify = await runWorkflow(() => ({}), {
    args: { phase: 'verify', run_id: 'R', objective: 'o', schema: SCHEMA, agent_fetch: 4,
            batches: [[{ relation_id: 'L-1' }]] },
    path: path('verify'),
  })
  assert.ok(!verify.calls[0].prompt.includes('fetch_paper'))
})

test('literature: one agent per question, schema and model passed, outputs tagged by agent', async () => {
  const { result, calls } = await runWorkflow(label => ({ claims: [{ ref: 'c1', from: label }] }),
    { args: literatureArgs(), path: path('literature') })
  assert.deepEqual(calls.map(c => c.label), ['literature-1', 'literature-2'])
  assert.ok(calls.every(c => c.schema === SCHEMA && c.model === 'claude-haiku-4-5'))
  assert.ok(calls[1].prompt.includes('Which cytokines?') && calls[1].prompt.includes('character for character'))
  assert.ok(calls[0].prompt.includes('mcp__literature__check_quote') && calls[0].prompt.includes('`near`'))
  assert.ok(calls[0].prompt.includes('at most 6 web searches and 6 page fetches') && calls[0].prompt.includes('answer with what you have'))
  assert.deepEqual(result, { phase: 'literature', dropped: [],
    outputs: [{ agent_id: 'literature-1', output: { claims: [{ ref: 'c1', from: 'literature-1' }] } },
              { agent_id: 'literature-2', output: { claims: [{ ref: 'c1', from: 'literature-2' }] } }] })
})

test('an agent that returns nothing is listed as dropped and logged, not hidden', async () => {
  const { result, logs } = await runWorkflow(label => (label === 'literature-2' ? null : { claims: [] }),
    { args: literatureArgs(), path: path('literature') })
  assert.deepEqual(result.dropped, ['literature-2'])
  assert.equal(result.outputs.length, 1)
  assert.ok(logs.some(l => l.includes('literature-2')))
})

test('generate shows the prior causal graph, or says plainly that there is none', async () => {
  const base = { phase: 'generate', run_id: 'R', objective: 'o', schema: SCHEMA,
                 perspectives: ['immunology'], kb_context: [] }
  const withPrior = await runWorkflow(() => ({}), {
    args: { ...base, prior_graph: [{ claim_id: 'C-7', cause: 'expansion', effect: 'neuron loss',
                                     sign: 'negative', mechanism: 'DPR toxicity', status: 'unresolved' }] },
    path: path('generate'),
  })
  assert.ok(withPrior.calls[0].prompt.includes('"C-7"'), 'the prior edge must reach the prompt')
  assert.ok(withPrior.calls[0].prompt.includes('1 edge(s)'))
  assert.ok(withPrior.calls[0].prompt.includes('**not** a causal model'))   // the line wraps after 'model'
  // An empty prior is stated, so an author does not assume a scaffold exists.
  const empty = await runWorkflow(() => ({}), { args: { ...base, prior_graph: [] }, path: path('generate') })
  assert.ok(empty.calls[0].prompt.includes('Prior causal graph: empty'))
})

test('the knowledge-base slice reaches the prompt', async () => {
  const kb = [{ id: 'C-1', content: 'CD4 responses are increased', epistemic_status: 'unresolved' }]
  const { calls } = await runWorkflow(() => ({ hypothesis: {} }), {
    args: { phase: 'generate', run_id: 'RUN-1', objective: 'o', schema: SCHEMA, kb_context: kb,
            perspectives: ['immunology', 'neurodegeneration', 'genetics'] },
    path: path('generate'),
  })
  assert.deepEqual(calls.map(c => c.label), ['author-1', 'author-2', 'author-3'])
  assert.ok(calls.every(c => c.prompt.includes('"C-1"') && /never invent\s+one/.test(c.prompt)))
  assert.ok(calls[2].prompt.includes('Your angle: genetics'))
  assert.ok(calls[0].prompt.includes('no web access'))
})

test('an empty slice says no records were packed, never that the knowledge base is empty', async () => {
  // `literature` is handed `kb_context: []` on purpose (pipeline/orchestrator.py), while the store may hold
  // many claims: the sentence its agents read must be true of the prompt.
  const { calls } = await runWorkflow(() => ({ claims: [] }), { args: literatureArgs(), path: path('literature') })
  assert.ok(calls.every(c => !c.prompt.includes('holds nothing relevant yet')))
  assert.ok(calls.every(c => c.prompt.includes('does not mean the knowledge base is empty')))
  assert.ok(calls.every(c => c.prompt.includes('mcp__kb__kb_search')))     // and it names how to reach the store
  // A slice, when there is one, still reaches the prompt and the empty wording is gone.
  const withSlice = await runWorkflow(() => ({ claims: [] }),
    { args: literatureArgs({ kb_context: [{ id: 'C-9', content: 'a recorded claim' }] }), path: path('literature') })
  assert.ok(withSlice.calls[0].prompt.includes('"C-9"'))
  assert.ok(!withSlice.calls[0].prompt.includes('does not mean the knowledge base is empty'))
})

test('the ID rule forbids inventing an ID, not citing one a tool returned', async () => {
  // "Cite only IDs present below" would contradict `kbBlock()`, which tells the same agent in the same prompt
  // that a record a tool returns may be cited.
  const generate = await runWorkflow(() => ({}), {
    args: { phase: 'generate', run_id: 'R', objective: 'o', schema: SCHEMA, perspectives: ['immunology'],
            kb_context: [{ id: 'C-1' }] },
    path: path('generate'),
  })
  const review = await runWorkflow(() => ({}), {
    args: { phase: 'review', run_id: 'R', objective: 'o', schema: SCHEMA, reviewers: 2,
            items: [{ hypothesis_id: 'H-1', context: [{ id: 'C-3' }] }] },
    path: path('review'),
  })
  for (const prompt of [generate.calls[0].prompt, review.calls[0].prompt]) {
    assert.ok(!/Cite only IDs present/.test(prompt), 'no blanket ban on IDs outside the slice')
    assert.ok(/never invent\s+one/.test(prompt), 'the rule those sentences protected must survive')
    assert.ok(prompt.includes("knowledge-base tool's result"), 'a tool result is a place an ID may come from')
    assert.ok(prompt.includes('may be cited by ID exactly like one quoted below'), 'kbBlock is no longer contradicted')
  }
})

test('a phase refuses args meant for another phase or missing fields', async () => {
  await assert.rejects(runWorkflow(() => ({}), { args: literatureArgs({ phase: 'generate' }), path: path('literature') }),
    /args.phase is generate/)
  await assert.rejects(runWorkflow(() => ({}), { args: literatureArgs({ questions: [] }), path: path('literature') }),
    /questions must be a non-empty list/)
  await assert.rejects(runWorkflow(() => ({}), { args: literatureArgs({ schema: undefined }), path: path('literature') }),
    /args.schema is missing/)
})

test('review: every hypothesis gets `reviewers` independent reviewers, outputs tagged with the hypothesis', async () => {
  const items = [{ hypothesis_id: 'H-1', context: [{ id: 'C-3' }] }, { hypothesis_id: 'H-2', context: [] }]
  const { result, calls } = await runWorkflow(label => (label === 'reviewer-2@H-2' ? null : { summary: label }), {
    args: { phase: 'review', run_id: 'R', objective: 'o', schema: SCHEMA, reviewers: 2, items },
    path: path('review'),
  })
  assert.deepEqual(calls.map(c => c.label), ['reviewer-1@H-1', 'reviewer-2@H-1', 'reviewer-1@H-2', 'reviewer-2@H-2'])
  assert.ok(calls[0].prompt.includes('"C-3"') && calls[0].prompt.includes('do not know who wrote it'))
  assert.deepEqual(result.outputs.map(o => o.hypothesis_id), ['H-1', 'H-1', 'H-2'])
  assert.deepEqual(result.dropped, ['reviewer-2@H-2'])
})

test('review refuses a single reviewer', async () => {
  await assert.rejects(runWorkflow(() => ({}), {
    args: { phase: 'review', run_id: 'R', objective: 'o', schema: SCHEMA, reviewers: 1, items: [{ hypothesis_id: 'H-1' }] },
    path: path('review'),
  }), /reviewers must be an integer >= 2/)
})

test('verify: one verifier per batch, judging on the quote without the authors\' rationale', async () => {
  const batches = [[{ relation_id: 'L-1', source: { quote: 'q1' } }], [{ relation_id: 'L-2', source: { quote: 'q2' } }]]
  const { result, calls } = await runWorkflow(() => ({ verdicts: [] }), {
    args: { phase: 'verify', run_id: 'R', objective: 'o', schema: SCHEMA, batches },
    path: path('verify'),
  })
  assert.deepEqual(calls.map(c => c.label), ['verifier-1', 'verifier-2'])
  assert.ok(calls[1].prompt.includes('"L-2"') && !calls[1].prompt.includes('"L-1"'))
  assert.ok(calls[0].prompt.includes('Judge on the quote alone'))
  assert.equal(result.outputs.length, 2)
})

// 31 claims and their relations were refused across nine runs because the agents typed the evidence kind and
// filled the causal block, which the old enum forbade. The block and the type are now independent, and the
// three prompts that write claims say so.
test('the claim-writing phases say the causal block and the type are separate questions', async () => {
  const fixtures = {
    literature: { phase: 'literature', run_id: 'R', objective: 'o', schema: SCHEMA, questions: ['q'],
      max_claims: 5, web: { searches: 2, fetches: 2 } },
    generate: { phase: 'generate', run_id: 'R', objective: 'o', schema: SCHEMA, perspectives: ['immunology'],
      kb_context: [] },
    review: { phase: 'review', run_id: 'R', objective: 'o', schema: SCHEMA, reviewers: 2,
      items: [{ hypothesis_id: 'H-1', context: [], targets: [], gaps: [] }] },
  }
  for (const [name, args] of Object.entries(fixtures)) {
    const { calls } = await runWorkflow(() => ({ claims: [], links: [], gaps: [], summary: 's' }),
      { args, path: path(name) })
    const prompt = calls[0].prompt
    assert.ok(prompt.includes('all four or none') || prompt.includes('All four or none'),
      `${name}: the block is all-or-nothing`)
    // ADR 0032: no sign; the direction of the effect is in the entity names.
    assert.ok(!/\bsign\b/.test(prompt), `${name}: the prompt asks for no sign`)
    const flat = prompt.replace(/\s+/g, ' ')
    assert.ok(flat.includes('`type` is a separate question') || flat.includes('`type` says separately'),
      `${name}: the type is a separate question`)
  }
})

// A verdict that confirms the relation while judging the claim overstated used to be discarded whole: 34 of
// them across nine runs, every confirmed contradiction of the two production runs among them. The prompt now
// asks for the restatement that keeps the evidence (`Builder.narrowed`).
test('verify: the verifier is asked to narrow an overstated claim to its quote, and only then', async () => {
  const { calls } = await runWorkflow(() => ({ verdicts: [] }), {
    args: { phase: 'verify', run_id: 'R', objective: 'o', schema: SCHEMA,
      batches: [[{ relation_id: 'L-1', source: { quote: 'q1' } }]] },
    path: path('verify'),
  })
  const prompt = calls[0].prompt
  assert.ok(prompt.includes('`narrowed`'), 'the field is named')
  assert.ok(prompt.includes('only** when the verdict is `confirmed` and `faithful` is false'), 'and bounded')
  assert.ok(prompt.includes('interpret nothing, name no entity the quote does not name'),
    'the restatement may not add anything')
})

const hypothesisItems = [{ hypothesis_id: 'H-1', context: [{ id: 'C-3' }], targets: ['C-3'], gaps: ['I-1'] },
  { hypothesis_id: 'H-2', context: [], targets: [], gaps: [] }]

test('design, arbitrate and intervene: one agent per hypothesis, outputs tagged, no web', async () => {
  for (const [name, prefix] of [['design', 'designer'], ['arbitrate', 'arbiter'], ['intervene', 'intervener']]) {
    const { result, calls } = await runWorkflow(() => ({ ok: true }), {
      args: { phase: name, run_id: 'R', objective: 'o', schema: SCHEMA, items: hypothesisItems, web: { searches: 0, fetches: 0 } },
      path: path(name),
    })
    assert.deepEqual(calls.map(c => c.label), [`${prefix}@H-1`, `${prefix}@H-2`])
    assert.deepEqual(result.outputs.map(o => o.hypothesis_id), ['H-1', 'H-2'])
    assert.ok(calls[0].prompt.includes('no web access'))
  }
})

test('design aims at the unresolved claims and gaps and demands a falsifying outcome', async () => {
  const { calls } = await runWorkflow(() => ({}), {
    args: { phase: 'design', run_id: 'R', objective: 'o', schema: SCHEMA, items: hypothesisItems.slice(0, 1) },
    path: path('design'),
  })
  assert.ok(calls[0].prompt.includes('["C-3"]') && calls[0].prompt.includes('["I-1"]'))
  assert.ok(calls[0].prompt.includes('at least one must contradict'))
})

test('design offers the in-silico kind and forbids claiming what a dataset contains', async () => {
  const { calls } = await runWorkflow(() => ({}), {
    args: { phase: 'design', run_id: 'R', objective: 'o', schema: SCHEMA, items: hypothesisItems.slice(0, 1) },
    path: path('design'),
  })
  assert.ok(calls[0].prompt.includes('`in_silico` if the test is a computation over a dataset'))
  assert.ok(calls[0].prompt.includes('may NOT state what a dataset contains'))
  assert.ok(calls[0].prompt.includes('`required_markers` and `required_metadata`'))
})

test('arbitrate distinguishes available measurements from executable analysis', async () => {
  const { calls } = await runWorkflow(() => ({}), {
    args: { phase: 'arbitrate', run_id: 'R', objective: 'o', schema: SCHEMA, items: hypothesisItems.slice(0, 1) },
    path: path('arbitrate'),
  })
  assert.ok(calls[0].prompt.includes('`data_supported`, `missing_inputs`, or `unverified`'))
  assert.ok(calls[0].prompt.includes('not that code exists to run the analysis'))
})

test('arbitrate treats gaps as reasons to test, not to reject', async () => {
  const { calls } = await runWorkflow(() => ({}), {
    args: { phase: 'arbitrate', run_id: 'R', objective: 'o', schema: SCHEMA, items: hypothesisItems.slice(0, 1) },
    path: path('arbitrate'),
  })
  assert.ok(calls[0].prompt.includes('is a reason to test, not to reject'))
})

test('intervene allows no candidate, demands stated risks and frames candidates as research, not advice', async () => {
  const { calls } = await runWorkflow(() => ({}), {
    args: { phase: 'intervene', run_id: 'R', objective: 'o', schema: SCHEMA, items: hypothesisItems.slice(0, 1) },
    path: path('intervene'),
  })
  assert.ok(calls[0].prompt.includes('no_candidate_reason'))
  assert.ok(calls[0].prompt.includes('an unstated risk is not evidence of safety'))
  assert.ok(calls[0].prompt.includes('never a treatment recommendation'))
})

test('synthesize: one PI over all hypotheses and decisions, allowed to conclude insufficient evidence', async () => {
  const { result, calls } = await runWorkflow(() => ({ outcome: 'insufficient_evidence' }), {
    args: { phase: 'synthesize', run_id: 'R', objective: 'o', schema: SCHEMA,
            items: [{ hypotheses: hypothesisItems, decisions: [{ id: 'D-1', decision: 'stop' }] }] },
    path: path('synthesize'),
  })
  assert.deepEqual(calls.map(c => c.label), ['pi'])
  assert.ok(calls[0].prompt.includes('"D-1"') && calls[0].prompt.includes('insufficient_evidence'))
  // The PI is told the citation format, because a malformed tag is rewritten or fails the gate.
  assert.ok(calls[0].prompt.includes('One ID per tag') && calls[0].prompt.includes('not [[C-1, C-2]]'))
  // A tool name inside a tag is a tag holding no ID: a run wrote [[mcp__kb__kb_mechanism_path]] and failed the gate.
  assert.ok(calls[0].prompt.includes('Name tools only between backticks, never in [[ ]]'))
  // The PI must use the manifest check and distinguish available data from an executable test.
  assert.ok(calls[0].prompt.includes('deterministic feasibility results'))
  assert.ok(calls[0].prompt.includes('`runnable_now_experiment_ids`'))
  // The five section checks of `pipeline/section_gates.py` are stated to the PI, with their own remedy.
  for (const required of ['would *contradict* a claim', '`stratification` field', '`biomarker` field',
                          'two different kinds', 'say that in the section']) {
    assert.ok(calls[0].prompt.includes(required), required)
  }
  // Section A is the check the prompt did not state at all before a fix, which is why one run cited
  // eight causal claims that shared no node while its own KB held a six-edge chain.
  for (const required of ['**A** must cite a chain of *proper* causal claims', 'A mechanism is a chain',
                          '`mcp__kb__kb_mechanism_path`', 'holds no proper causal edge']) {
    assert.ok(calls[0].prompt.includes(required), required)
  }
  // Every phase is told that one tool answers with a set of edges and the other with a connected chain.
  assert.ok(calls[0].prompt.includes('a list of causes and not a pathway'))
  assert.equal(result.outputs[0].output.outcome, 'insufficient_evidence')
  await assert.rejects(runWorkflow(() => ({}), {
    args: { phase: 'synthesize', run_id: 'R', objective: 'o', schema: SCHEMA, items: [] }, path: path('synthesize') }),
  /exactly one item/)
})

// Six phases are told that computed evidence exists; literature and verify are not (literature writes claims
// from sources, verify judges a quote).
test('the phases that reason over evidence are told what a computed claim is', async () => {
  const named = {
    generate: { phase: 'generate', run_id: 'R', objective: 'o', schema: SCHEMA, perspectives: ['immunology'], kb_context: [] },
    review: { phase: 'review', run_id: 'R', objective: 'o', schema: SCHEMA, reviewers: 2, items: hypothesisItems.slice(0, 1) },
    design: { phase: 'design', run_id: 'R', objective: 'o', schema: SCHEMA, items: hypothesisItems.slice(0, 1) },
    arbitrate: { phase: 'arbitrate', run_id: 'R', objective: 'o', schema: SCHEMA, items: hypothesisItems.slice(0, 1) },
    intervene: { phase: 'intervene', run_id: 'R', objective: 'o', schema: SCHEMA, items: hypothesisItems.slice(0, 1) },
    synthesize: { phase: 'synthesize', run_id: 'R', objective: 'o', schema: SCHEMA,
                  items: [{ hypotheses: hypothesisItems, decisions: [] }] },
    verify: { phase: 'verify', run_id: 'R', objective: 'o', schema: SCHEMA, batches: [[{ relation_id: 'L-1' }]] },
    literature: literatureArgs(),
  }
  for (const [name, a] of Object.entries(named)) {
    const { calls } = await runWorkflow(() => ({}), { args: a, path: path(name) })
    const told = calls[0].prompt.includes('`origin: computation`')
    assert.equal(told, !['verify', 'literature'].includes(name), `${name}: told = ${told}`)
    if (told) assert.ok(calls[0].prompt.includes('re-running that analysis on those inputs reproduces the number'))
  }
})

// The data tools are granted alongside the KB ones; `verify` again is left out, and no prompt may invite a read
// of a data file (reading belongs to code that writes a verifiable artifact).
test('every phase but verify tells its agents the read-only data tools exist, metadata only', async () => {
  const args = {
    literature: literatureArgs(),
    generate: { phase: 'generate', run_id: 'R', objective: 'o', schema: SCHEMA, perspectives: ['immunology'], kb_context: [] },
    review: { phase: 'review', run_id: 'R', objective: 'o', schema: SCHEMA, reviewers: 2, items: hypothesisItems.slice(0, 1) },
    verify: { phase: 'verify', run_id: 'R', objective: 'o', schema: SCHEMA, batches: [[{ relation_id: 'L-1' }]] },
    design: { phase: 'design', run_id: 'R', objective: 'o', schema: SCHEMA, items: hypothesisItems.slice(0, 1) },
    arbitrate: { phase: 'arbitrate', run_id: 'R', objective: 'o', schema: SCHEMA, items: hypothesisItems.slice(0, 1) },
    intervene: { phase: 'intervene', run_id: 'R', objective: 'o', schema: SCHEMA, items: hypothesisItems.slice(0, 1) },
    synthesize: { phase: 'synthesize', run_id: 'R', objective: 'o', schema: SCHEMA,
                  items: [{ hypotheses: hypothesisItems, decisions: [] }] },
  }
  for (const [name, a] of Object.entries(args)) {
    const { calls } = await runWorkflow(() => ({}), { args: a, path: path(name) })
    const mentions = calls[0].prompt.includes('mcp__data__data_list')
    assert.equal(mentions, name !== 'verify', `${name}: data tools mentioned = ${mentions}`)
    if (mentions) assert.ok(calls[0].prompt.includes('metadata only, never the contents'))
  }
})

// The KB tools are granted to every phase by the launcher, but an agent only uses a tool its prompt names.
// `verify` is left out on purpose: its verdict must rest on the quote it is handed, not on the KB.
test('every phase but verify tells its agents the read-only KB tools exist', async () => {
  const args = {
    literature: literatureArgs(),
    generate: { phase: 'generate', run_id: 'R', objective: 'o', schema: SCHEMA, perspectives: ['immunology'], kb_context: [] },
    review: { phase: 'review', run_id: 'R', objective: 'o', schema: SCHEMA, reviewers: 2, items: hypothesisItems.slice(0, 1) },
    verify: { phase: 'verify', run_id: 'R', objective: 'o', schema: SCHEMA, batches: [[{ relation_id: 'L-1' }]] },
    design: { phase: 'design', run_id: 'R', objective: 'o', schema: SCHEMA, items: hypothesisItems.slice(0, 1) },
    arbitrate: { phase: 'arbitrate', run_id: 'R', objective: 'o', schema: SCHEMA, items: hypothesisItems.slice(0, 1) },
    intervene: { phase: 'intervene', run_id: 'R', objective: 'o', schema: SCHEMA, items: hypothesisItems.slice(0, 1) },
    synthesize: { phase: 'synthesize', run_id: 'R', objective: 'o', schema: SCHEMA,
                  items: [{ hypotheses: hypothesisItems, decisions: [] }] },
  }
  for (const [name, a] of Object.entries(args)) {
    const { calls } = await runWorkflow(() => ({}), { args: a, path: path(name) })
    const mentions = calls[0].prompt.includes('mcp__kb__kb_search')
    assert.equal(mentions, name !== 'verify', `${name}: KB tools mentioned = ${mentions}`)
    if (mentions) assert.ok(calls[0].prompt.includes('never invent an ID'))
    // Searching for a tool is not calling it: an agent can loop on ToolSearch.
    if (mentions) assert.ok(calls[0].prompt.includes('searching for it again is a wasted turn'))
  }
})

test('novelty gives one agent per hypothesis only that hypothesis and the search tool', async () => {
  const { calls } = await runWorkflow(() => ({}), {
    args: { phase: 'novelty', run_id: 'R', objective: 'o', schema: SCHEMA,
            items: [{ hypothesis_id: 'H-1', context: [{ id: 'C-1' }] }, { hypothesis_id: 'H-2', context: [] }] },
    path: path('novelty'),
  })
  assert.deepEqual(calls.map(c => c.label), ['novelty@H-1', 'novelty@H-2'])
  assert.ok(calls[0].prompt.includes('mcp__literature__search_works(query)'))
  assert.ok(calls[0].prompt.includes('no_prior_art_found is NOT a finding that the hypothesis is new'))
  assert.ok(calls[0].prompt.includes('Hypothesis H-1') && !calls[0].prompt.includes('Hypothesis H-2'))
  assert.ok(calls[0].prompt.includes('closest_works is a required field'))
})

test('novelty refuses a delivery with no hypothesis', async () => {
  await assert.rejects(runWorkflow(() => ({}), {
    args: { phase: 'novelty', run_id: 'R', objective: 'o', schema: SCHEMA, items: [] }, path: path('novelty') }))
})
