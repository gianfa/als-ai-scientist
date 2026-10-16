import { fill, agentOptions, collect, requirePhaseArgs } from './lib/phase.js'
import ROLE from '../../../agents/novelty.md'

export const meta = {
  name: 'phase-novelty',
  description: 'For each hypothesis of a finished run, a search for the closest published work. Run after the pipeline.',
  phases: [{ title: 'novelty' }],
}

const INJECTED_ARGS = null // @@INJECTED_ARGS@@

const A = requirePhaseArgs(INJECTED_ARGS ?? args, 'novelty', ['items'])
if (!Array.isArray(A.items) || A.items.length === 0) throw new Error('novelty: no hypothesis to look for')
phase('novelty')

// One agent per hypothesis, so one hypothesis's search cannot steer another's. The agent is given the hypothesis
// and its claims, never an opinion on whether it is new: the verdict has to come from works it has read.
// Abstracts decide little: a work can state a mechanism only in its body. With a reading budget the agent opens the
// closest papers; without one it works on abstracts alone and must say so. The host checks the claim against the
// server's own log, so "I read it" is never taken on trust.
const reading = A.max_papers_read > 0
  ? `You may open the full text of up to ${A.max_papers_read} different papers: take the closest ones, call
mcp__literature__fetch_paper(identifier) with the DOI a search returned, then mcp__literature__read_paper(identifier,
offset) page by page. A paper that comes back paywalled cannot be read: say so in the limitations and do not list it as
read. List the DOI of every paper you actually opened in full_texts_read.`
  : 'You have no tool to open a full text: you work on abstracts alone, say so in the limitations, and leave full_texts_read empty.'

const prompt = item => fill(ROLE, {
  reading: reading,
  hypothesis_id: item.hypothesis_id,
  context: JSON.stringify(item.context, null, 1),
})

const ids = A.items.map(item => `novelty@${item.hypothesis_id}`)
const results = await parallel(A.items.map((item, i) => () => agent(prompt(item), agentOptions(A, ids[i]))))
return collect('novelty', ids, results)
