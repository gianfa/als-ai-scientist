import { fill, agentOptions, collect, requirePhaseArgs } from './lib/phase.js'
import ROLE from '../../../agents/reassess.md'

export const meta = {
  name: 'phase-reassess',
  description: 'An independent post-result judgement of each executed experiment and the hypothesis it tested.',
  phases: [{ title: 'reassess' }],
}

const INJECTED_ARGS = null // @@INJECTED_ARGS@@

const A = requirePhaseArgs(INJECTED_ARGS ?? args, 'reassess', ['executions'])
if (!Array.isArray(A.executions) || A.executions.length === 0) throw new Error('reassess: no executed result')
for (const e of A.executions) {
  if (e?.execution?.status !== 'executed') throw new Error('reassess: every entry must be an executed result')
}
phase('reassess')

// One critic per executed EXP, labelled by EXP and not by hypothesis: two EXPs of one run can test the same
// hypothesis (an earlier run: H-1 bought both EXP-1 and EXP-2), and a label that collided would lose a judgement.
// One computation can serve several hypotheses: the host runs it once and binds every EXP that bought
// it to that result, so each critic is told the measurement is shared and is not a replication.
const shared = execution => (execution.shared_with ?? []).length === 0 ? '' : `

This result is one measurement shared with ${execution.shared_with.map(o => `${o.experiment_id} (${o.hypothesis_id})`).join(', ')}:
the same analysis on the same inputs, computed once. Judge what it does to this hypothesis only. Other critics'
judgements of the same result are not independent replications, and neither is this one.`

const prompt = (execution, hypothesis) => fill(ROLE, {
  experiment_id: execution.experiment_id,
  shared_with: shared(execution),
  tested_claims: JSON.stringify(execution.tested_claims),
  citable_context_claims: JSON.stringify(hypothesis.citable_context_claims),
  execution: JSON.stringify(execution, null, 1),
  hypothesis: JSON.stringify(hypothesis, null, 1),
})

const ids = A.executions.map(entry => `reassessor@${entry.execution.experiment_id}`)
const results = await parallel(A.executions.map((entry, i) => () =>
  agent(prompt(entry.execution, entry.hypothesis), agentOptions(A, ids[i]))))
return collect('reassess', ids, results)
