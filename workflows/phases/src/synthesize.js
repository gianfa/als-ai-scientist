import { fill, agentOptions, collect, computedBlock, dataBlock, kbBlock, requirePhaseArgs, webBlock } from './lib/phase.js'
import ROLE from '../../../agents/synthesize.md'

export const meta = {
  name: 'phase-synthesize',
  description: 'Synthesize phase: the principal investigator writes the final research proposal (outputs A-H) from the knowledge base.',
  phases: [{ title: 'synthesize' }],
}

const INJECTED_ARGS = null // @@INJECTED_ARGS@@

const A = requirePhaseArgs(INJECTED_ARGS ?? args, 'synthesize', ['items'])
if (!Array.isArray(A.items) || A.items.length !== 1) throw new Error('synthesize: args.items must hold exactly one item')

phase('synthesize')

const item = A.items[0]
const prompt = fill(ROLE, {
  objective: A.objective,
  web_tools: webBlock(A.web),
  computed_claims: computedBlock(),
  kb_tools: kbBlock(),
  data_tools: dataBlock(),
  decisions: JSON.stringify(item.decisions, null, 1),
  experiment_feasibility: JSON.stringify(A.experiment_feasibility ?? {}, null, 1),
  execution: JSON.stringify(A.execution ?? { status: 'not_executed' }, null, 1),
  reassessment: JSON.stringify(A.reassessment ?? { status: 'not_reassessed' }, null, 1),
  hypotheses: JSON.stringify(item.hypotheses, null, 1),
})

const result = await agent(prompt, agentOptions(A, 'pi'))
return collect('synthesize', ['pi'], [result])
