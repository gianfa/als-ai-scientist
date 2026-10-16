// What every phase's agents are told, fixed byte for byte.
//
// Each case runs a built phase with fixed arguments and compares the prompts its agents receive with the file in
// tests/workflow/golden/<phase>.json. The cases cover every branch of the prompt text (a budget present or absent,
// a context empty or not, a registered analysis or not), so a change to where a prompt is written cannot change
// what an agent reads without failing here. To accept an intended change of wording, regenerate with
// UPDATE_GOLDEN=1 and review the diff of the golden files.

import assert from 'node:assert/strict'
import { readFileSync, writeFileSync } from 'node:fs'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'

import { runWorkflow } from './harness.js'

const path = name => fileURLToPath(new URL(`../../workflows/phases/${name}.js`, import.meta.url))
const golden = name => fileURLToPath(new URL(`./golden/${name}.json`, import.meta.url))
const UPDATE = process.env.UPDATE_GOLDEN === '1'

const SCHEMA = { type: 'object', properties: { claims: { type: 'array' } }, required: ['claims'] }
const base = phase => ({ phase, run_id: 'RUN-G', objective: 'C9orf72 autoimmunity in ALS', schema: SCHEMA })
const WEB = { searches: 6, fetches: 4 }
const NO_WEB = { searches: 0, fetches: 0 }
const CONTEXT = [{ id: 'C-3', text: 'CD4+ T cells respond to C9orf72 epitopes', status: 'verified' }]
const ITEMS = [
  { hypothesis_id: 'H-1', context: CONTEXT, targets: ['C-3'], gaps: ['I-1'],
    experiment_feasibility: { 'EXP-1': { verdict: 'data_supported' } } },
  { hypothesis_id: 'H-2', context: [], targets: [], gaps: [] },
]
const EXECUTION = { status: 'executed', experiment_id: 'EXP-1', hypothesis_id: 'H-1', tested_claims: ['C-3'] }
const HYPOTHESIS = { hypothesis_id: 'H-1', citable_context_claims: ['C-3'] }

const CASES = {
  literature: {
    bare: { ...base('literature'), questions: ['Which T cells respond to C9orf72?'], kb_context: [] },
    full: { ...base('literature'), questions: ['Which T cells respond?', 'Which cytokines?'], kb_context: CONTEXT,
            max_claims: 5, agent_fetch: 8, web: WEB },
  },
  generate: {
    bare: { ...base('generate'), perspectives: ['immunology'], kb_context: [] },
    full: { ...base('generate'), perspectives: ['immunology', 'therapeutics'], kb_context: CONTEXT, web: WEB,
            prior_graph: [{ cause: 'IL-10', effect: 'survival', sign: '+' }] },
  },
  review: {
    bare: { ...base('review'), reviewers: 2, items: ITEMS.slice(0, 1) },
    full: { ...base('review'), reviewers: 2, items: ITEMS, agent_fetch: 4, web: WEB },
  },
  verify: {
    bare: { ...base('verify'), batches: [[{ relation_id: 'L-1' }]] },
    full: { ...base('verify'), batches: [[{ relation_id: 'L-1' }], [{ relation_id: 'L-2' }]], web: WEB },
  },
  design: {
    bare: { ...base('design'), items: ITEMS, web: NO_WEB },
    full: { ...base('design'), items: ITEMS, web: WEB },
  },
  arbitrate: {
    bare: { ...base('arbitrate'), items: ITEMS, web: NO_WEB },
    full: { ...base('arbitrate'), items: ITEMS, web: WEB },
  },
  execute: {
    registered: { ...base('execute'), selected: [{ experiment_id: 'EXP-1', analysis_id: 'hla_panel_compact_v1' },
                                                 { experiment_id: 'EXP-2', analysis_id: 'ics_cytokine_skew_v1' }] },
    unregistered: { ...base('execute'), selected: [{ experiment_id: 'EXP-3', analysis_id: 'not_a_handler' }] },
  },
  reassess: {
    alone: { ...base('reassess'), executions: [{ execution: EXECUTION, hypothesis: HYPOTHESIS }] },
    // A delivery without tested_claims: the prompt has always read "undefined" there, and must keep doing so.
    sparse: { ...base('reassess'), executions: [{ execution: { status: 'executed', experiment_id: 'EXP-6' },
                                                  hypothesis: { hypothesis_id: 'H-6' } }] },
    shared: { ...base('reassess'), executions: [{ execution: { ...EXECUTION, shared_with: [
      { experiment_id: 'EXP-4', hypothesis_id: 'H-2' }, { experiment_id: 'EXP-5', hypothesis_id: 'H-3' }] },
      hypothesis: HYPOTHESIS }] },
  },
  intervene: {
    bare: { ...base('intervene'), items: ITEMS, web: NO_WEB },
    full: { ...base('intervene'), items: ITEMS, web: WEB, execution: EXECUTION,
            reassessment: { status: 'judged', judgement: 'inconclusive' } },
  },
  synthesize: {
    bare: { ...base('synthesize'), items: [{ hypotheses: ITEMS, decisions: [] }] },
    full: { ...base('synthesize'), items: [{ hypotheses: ITEMS, decisions: [{ decision_id: 'D-1' }] }], web: WEB,
            experiment_feasibility: { 'EXP-1': { verdict: 'data_supported' } }, execution: EXECUTION,
            reassessment: { status: 'judged', judgement: 'keep' } },
  },
  novelty: {
    abstracts: { ...base('novelty'), items: ITEMS },
    reading: { ...base('novelty'), items: ITEMS.slice(0, 1), max_papers_read: 3 },
  },
}

async function render(name) {
  const out = {}
  for (const [label, args] of Object.entries(CASES[name])) {
    const { calls } = await runWorkflow(() => ({}), { args: structuredClone(args), path: path(name) })
    out[label] = calls.map(c => ({ label: c.label, prompt: c.prompt }))
  }
  return out
}

for (const name of Object.keys(CASES)) {
  test(`${name}: every prompt is byte-identical to its golden file`, async () => {
    const actual = await render(name)
    if (UPDATE) writeFileSync(golden(name), JSON.stringify(actual, null, 1) + '\n')
    const expected = JSON.parse(readFileSync(golden(name), 'utf8'))
    assert.deepEqual(actual, expected)
  })
}
