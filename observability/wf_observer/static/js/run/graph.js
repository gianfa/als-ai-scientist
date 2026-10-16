// What the selected run actually executed, phase by phase.
//
// Which agents really started, which returned, and in what order — read from the run's journals via
// `GET /runs/<id>/events`. The roles behind them (instructions, settings) are in the Agents view and the
// chain of phases in the Phase graph view; both read the same run's archive.

import { runEvents } from '../api.js'
import { el, showError } from '../dom.js'
import { causalGraphButton } from './causal_graph.js'

const STARTED = 'agent_started'
const FINISHED = 'agent_finished'

/** Group the event stream into phases, preserving the order each phase first appeared in. */
function phasesOf(events) {
  const phases = new Map()
  for (const event of events) {
    if (event.kind !== STARTED && event.kind !== FINISHED) continue
    const name = event.phase ?? 'unphased'
    if (!phases.has(name)) phases.set(name, new Map())
    const agents = phases.get(name)
    const key = event.agent_id ?? event.agent ?? String(agents.size)
    const agent = agents.get(key) ?? { label: event.agent ?? key, finished: false, summary: null, causalGraph: null }
    if (event.agent) agent.label = event.agent
    if (event.kind === FINISHED) {
      agent.finished = true
      agent.summary = event.summary
      agent.causalGraph = event.causal_graph ?? null
    }
    agents.set(key, agent)
  }
  return phases
}

function agentRow(agent) {
  // An agent that started and never returned is the signature of a killed or crashed run, so it is
  // shown as unfinished rather than omitted.
  return el('li', { class: agent.finished ? 'agent-done' : 'agent-running' },
    el('strong', {}, agent.label),
    agent.finished ? ' ✓' : ' … still running when the journal ended',
    agent.summary ? el('div', { class: 'muted' }, agent.summary) : null)
}

// The workflow runs exactly one causal-graph agent, so its graph belongs to the phase heading rather
// than to a list row. If a phase ever held several, only the first would be offered here.
function phaseGraphButton(agents) {
  const graph = [...agents.values()].find(a => a.causalGraph)?.causalGraph
  return graph ? el('div', {}, causalGraphButton(graph)) : null
}

// A slower earlier request must not paint over a newer render (main.js only guards the start).
let latest = 0

export async function render(container, { runId }) {
  const mine = ++latest
  try {
    const events = await runEvents(runId)
    if (mine !== latest) return
    const phases = phasesOf(events)

    if (phases.size === 0) {
      container.replaceChildren(el('div', { class: 'panel' },
        'This run recorded no agent events. Store runs and smoke runs have no workflow journal.'))
      return
    }

    container.replaceChildren(
      el('div', { class: 'panel' },
        el('h2', {}, 'Executed agents'),
        el('p', { class: 'muted' },
          'What this run did. The role of each phase (instructions, settings) is in the ',
          el('a', { href: `#run/${encodeURIComponent(runId)}/agents` }, 'Agents view'),
          '; the chain of phases with status and cost is in the ',
          el('a', { href: `#run/${encodeURIComponent(runId)}/flow` }, 'Phase graph'), '.'),
        ...[...phases].map(([name, agents]) => el('section', {},
          el('h3', {}, `${name} (${[...agents.values()].filter(a => a.finished).length}/${agents.size})`),
          phaseGraphButton(agents),
          el('ul', {}, [...agents.values()].map(agentRow))))))
  } catch (err) {
    showError(container, err)
  }
}
