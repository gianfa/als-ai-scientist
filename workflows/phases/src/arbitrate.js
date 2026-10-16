import { fill, computedBlock, dataBlock, kbBlock, perHypothesis, requirePhaseArgs, webBlock } from './lib/phase.js'
import ROLE from '../../../agents/arbitrate.md'

export const meta = {
  name: 'phase-arbitrate',
  description: 'Arbitrate phase: one independent arbiter per hypothesis decides whether its uncertainty is worth buying.',
  phases: [{ title: 'arbitrate' }],
}

const INJECTED_ARGS = null // @@INJECTED_ARGS@@

const A = requirePhaseArgs(INJECTED_ARGS ?? args, 'arbitrate', ['items'])
if (!Array.isArray(A.items) || A.items.length === 0) throw new Error('arbitrate: args.items must be a non-empty list')

phase('arbitrate')

function prompt(item) {
  return fill(ROLE, {
    objective: A.objective,
    hypothesis_id: item.hypothesis_id,
    web_tools: webBlock(A.web),
    computed_claims: computedBlock(),
    kb_tools: kbBlock(),
    data_tools: dataBlock(),
    context: JSON.stringify(item.context, null, 1),
    experiment_feasibility: JSON.stringify(item.experiment_feasibility ?? {}, null, 1),
  })
}

return await perHypothesis(A, 'arbiter', prompt)
