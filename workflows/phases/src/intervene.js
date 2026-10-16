import { fill, computedBlock, dataBlock, kbBlock, perHypothesis, requirePhaseArgs, webBlock } from './lib/phase.js'
import ROLE from '../../../agents/intervene.md'

export const meta = {
  name: 'phase-intervene',
  description: 'Intervene phase: one intervener per surviving hypothesis proposes candidate interventions with their risks, stratification and biomarker.',
  phases: [{ title: 'intervene' }],
}

const INJECTED_ARGS = null // @@INJECTED_ARGS@@

const A = requirePhaseArgs(INJECTED_ARGS ?? args, 'intervene', ['items'])
if (!Array.isArray(A.items) || A.items.length === 0) throw new Error('intervene: args.items must be a non-empty list')

phase('intervene')

function prompt(item) {
  return fill(ROLE, {
    objective: A.objective,
    hypothesis_id: item.hypothesis_id,
    web_tools: webBlock(A.web),
    computed_claims: computedBlock(),
    kb_tools: kbBlock(),
    data_tools: dataBlock(),
    execution: JSON.stringify(A.execution ?? { status: 'not_executed' }, null, 1),
    reassessment: JSON.stringify(A.reassessment ?? { status: 'not_reassessed' }, null, 1),
    context: JSON.stringify(item.context, null, 1),
  })
}

return await perHypothesis(A, 'intervener', prompt)
