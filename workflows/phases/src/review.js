import { fill, agentOptions, computedBlock, dataBlock, fetchBlock, kbBlock, requirePhaseArgs, webBlock } from './lib/phase.js'
import ROLE from '../../../agents/review.md'

export const meta = {
  name: 'phase-review',
  description: 'Review phase: independent, blinded reviewers attack each hypothesis with quoted evidence and declared gaps.',
  phases: [{ title: 'review' }],
}

const INJECTED_ARGS = null // @@INJECTED_ARGS@@

const A = requirePhaseArgs(INJECTED_ARGS ?? args, 'review', ['items', 'reviewers'])
if (!Array.isArray(A.items) || A.items.length === 0) throw new Error('review: args.items must be a non-empty list')
if (!Number.isInteger(A.reviewers) || A.reviewers < 2) throw new Error('review: args.reviewers must be an integer >= 2')

phase('review')

function prompt(item) {
  return fill(ROLE, {
    objective: A.objective,
    hypothesis_id: item.hypothesis_id,
    fetch_tools: fetchBlock(A.agent_fetch),
    web_tools: webBlock(A.web),
    computed_claims: computedBlock(),
    kb_tools: kbBlock(),
    data_tools: dataBlock(),
    context: JSON.stringify(item.context, null, 1),
  })
}

const tasks = A.items.flatMap(item => Array.from({ length: A.reviewers }, (_, r) => ({ item, id: `reviewer-${r + 1}@${item.hypothesis_id}` })))
const results = await parallel(tasks.map(t => () => agent(prompt(t.item), agentOptions(A, t.id))))
const outputs = []
const dropped = []
results.forEach((output, i) => {
  if (output) outputs.push({ agent_id: tasks[i].id, hypothesis_id: tasks[i].item.hypothesis_id, output })
  else dropped.push(tasks[i].id)
})
if (dropped.length) log(`review: ${dropped.length} reviewer(s) returned nothing: ${dropped.join(', ')}`)
return { phase: 'review', outputs, dropped }
