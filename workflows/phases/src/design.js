import { fill, computedBlock, dataBlock, kbBlock, perHypothesis, requirePhaseArgs, webBlock } from './lib/phase.js'
import ROLE from '../../../agents/design.md'

export const meta = {
  name: 'phase-design',
  description: 'Design phase: one designer per hypothesis proposes falsifying experiments aimed at its unresolved claims and gaps.',
  phases: [{ title: 'design' }],
}

const INJECTED_ARGS = null // @@INJECTED_ARGS@@

const A = requirePhaseArgs(INJECTED_ARGS ?? args, 'design', ['items'])
if (!Array.isArray(A.items) || A.items.length === 0) throw new Error('design: args.items must be a non-empty list')

phase('design')

function prompt(item) {
  return fill(ROLE, {
    objective: A.objective,
    hypothesis_id: item.hypothesis_id,
    targets: JSON.stringify(item.targets),
    gaps: JSON.stringify(item.gaps),
    web_tools: webBlock(A.web),
    computed_claims: computedBlock(),
    kb_tools: kbBlock(),
    data_tools: dataBlock(),
    context: JSON.stringify(item.context, null, 1),
  })
}

return await perHypothesis(A, 'designer', prompt)
