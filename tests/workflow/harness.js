// Run a Claude Code dynamic workflow script with mocked runtime globals, no LLM calls.
// It runs a built phase script (workflows/phases/<phase>.js), the same file the runtime receives.
//
// Runtime semantics, per https://code.claude.com/docs/en/workflows (read 2026-09-25):
// - agent() resolves to null if stopped or on an unrecoverable API error;
// - pipeline() keeps each null in the results array;
// - with `schema`, output is validated and retried (5 attempts), then the agent() call fails with an error.
// Assumed, NOT documented: parallel()/pipeline() results are in input order.

import { readFileSync } from 'node:fs'

// respond(label, prompt, options) returns the mocked agent output for one call.
export async function runWorkflow(respond, { args, path }) {
  const calls = []
  const logs = []
  const phases = []
  const agent = async (prompt, options = {}) => {
    calls.push({ label: options.label, prompt, schema: options.schema, model: options.model, agentType: options.agentType })
    return respond(options.label, prompt, options)
  }
  const parallel = thunks => Promise.all(thunks.map(t => t()))
  const pipeline = (items, fn) => Promise.all(items.map(fn))
  const phase = p => phases.push(p)
  const log = m => logs.push(m)

  // The built script is self-contained (templates inlined): only meta needs rewriting.
  const src = readFileSync(path, 'utf8')
  const AsyncFunction = (async () => {}).constructor
  const body = src.replace(/^export const meta\s*=/m, 'const meta =')
  const result = await new AsyncFunction('args', 'agent', 'parallel', 'pipeline', 'phase', 'log', body)(
    args, agent, parallel, pipeline, phase, log)
  return { result, calls, logs, phases, labels: calls.map(c => c.label) }
}
