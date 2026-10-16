import { fill, webBlock, agentOptions, collect, requirePhaseArgs } from './lib/phase.js'
import ROLE from '../../../agents/verify.md'

export const meta = {
  name: 'phase-verify',
  description: 'Verify phase: an independent verifier judges, on the quoted passage alone, the relations agents asserted.',
  phases: [{ title: 'verify' }],
}

const INJECTED_ARGS = null // @@INJECTED_ARGS@@

const A = requirePhaseArgs(INJECTED_ARGS ?? args, 'verify', ['batches'])
if (!Array.isArray(A.batches) || A.batches.length === 0 || A.batches.some(b => !Array.isArray(b) || b.length === 0)) {
  throw new Error('verify: args.batches must be a non-empty list of non-empty lists')
}

phase('verify')

function prompt(batch) {
  return fill(ROLE, {
    web_tools: webBlock(A.web),
    batch: JSON.stringify(batch, null, 1),
  })
}

const ids = A.batches.map((_, i) => `verifier-${i + 1}`)
const results = await parallel(A.batches.map((b, i) => () => agent(prompt(b), agentOptions(A, ids[i]))))
return collect('verify', ids, results)
