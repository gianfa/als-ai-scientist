import { fill, agentOptions, collect, computedBlock, contextBlock, dataBlock, kbBlock, priorBlock, requirePhaseArgs, paperBlock, webBlock } from './lib/phase.js'
import ROLE from '../../../agents/generate.md'

export const meta = {
  name: 'phase-generate',
  description: 'Generate phase: independent authors each propose one competing mechanistic hypothesis as atomic claims.',
  phases: [{ title: 'generate' }],
}

const INJECTED_ARGS = null // @@INJECTED_ARGS@@

const A = requirePhaseArgs(INJECTED_ARGS ?? args, 'generate', ['perspectives'])
if (!Array.isArray(A.perspectives) || A.perspectives.length === 0) throw new Error('generate: args.perspectives must be a non-empty list')

phase('generate')

function prompt(perspective) {
  return fill(ROLE, {
    objective: A.objective,
    perspective: perspective,
    web_tools: webBlock(A.web),
    computed_claims: computedBlock(),
    kb_tools: kbBlock(),
    data_tools: dataBlock(),
    paper_tools: paperBlock(),
    prior_graph: priorBlock(A.prior_graph),
    kb_context: contextBlock(A.kb_context),
  })
}

const ids = A.perspectives.map((_, i) => `author-${i + 1}`)
const results = await parallel(A.perspectives.map((p, i) => () => agent(prompt(p), agentOptions(A, ids[i]))))
return collect('generate', ids, results)
