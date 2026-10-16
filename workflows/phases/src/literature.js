import { fill, agentOptions, collect, contextBlock, dataBlock, fetchBlock, kbBlock, requirePhaseArgs, paperBlock, webBlock } from './lib/phase.js'
import ROLE from '../../../agents/literature.md'

export const meta = {
  name: 'phase-literature',
  description: 'Literature phase: one agent per question extracts atomic, quoted claims from primary sources.',
  phases: [{ title: 'literature' }],
}

const INJECTED_ARGS = null // @@INJECTED_ARGS@@

const A = requirePhaseArgs(INJECTED_ARGS ?? args, 'literature', ['questions'])
if (!Array.isArray(A.questions) || A.questions.length === 0) throw new Error('literature: args.questions must be a non-empty list')

phase('literature')

function prompt(question) {
  return fill(ROLE, {
    objective: A.objective,
    question: question,
    max_claims: A.max_claims ?? 8,
    fetch_tools: fetchBlock(A.agent_fetch),
    web_tools: webBlock(A.web),
    kb_tools: kbBlock(),
    data_tools: dataBlock(),
    paper_tools: paperBlock(),
    kb_context: contextBlock(A.kb_context),
  })
}

const ids = A.questions.map((_, i) => `literature-${i + 1}`)
const results = await parallel(A.questions.map((q, i) => () => agent(prompt(q), agentOptions(A, ids[i]))))
return collect('literature', ids, results)
