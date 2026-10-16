// Agents: the roles of the phase pipeline as this run used them, from `GET /runs/<id>/roles`.
//
// Per phase: the phase's own one-line description (archived in the run), the settings its workflow was
// launched with, the agents it started, and the role text `agents/<phase>.md`. The role text is the
// repository's current file; whether this run's agents received exactly that text is known only when the
// run's manifest recorded the role hashes (ADR 0031), and the view says which case applies.

import { runRoles } from '../api.js'
import { el, code, showError } from '../dom.js'
import * as state from '../state.js'

const value = v => (v !== null && typeof v === 'object' ? JSON.stringify(v) : String(v))

function settingsList(settings) {
  if (settings == null) return el('p', { class: 'sub' }, 'Launch arguments not archived.')
  const entries = Object.entries(settings)
  if (!entries.length) return el('p', { class: 'sub' }, 'No scalar settings among the launch arguments.')
  return el('dl', { class: 'kv' }, entries.flatMap(([k, v]) =>
    [el('dt', {}, k), el('dd', {}, Array.isArray(v) ? el('ul', {}, v.map(x => el('li', {}, x))) : code(value(v)))]))
}

function roleNote(r) {
  if (!r.role) return el('p', { class: 'sub' }, `No agents/${r.phase}.md in the repository.`)
  const state = !r.launched ? 'no agent of this run received it'
    : r.role_match === true ? 'matches the role hash this run recorded'
    : r.role_match === false ? el('span', { class: 'warn' }, 'differs from the role hash this run recorded: this run\'s agents received other text')
    : `this run recorded no role hash (it predates ADR 0031), so its agents' text is the one compiled into ${r.workflow_script ?? 'its workflow script'}, not necessarily this file`
  return el('details', {},
    el('summary', {}, `Role text: ${r.role.path} (repository now)`),
    el('p', { class: 'sub' }, 'sha256 ', code(r.role.sha256.slice(0, 12)), ' · ', state),
    el('pre', {}, r.role.text))
}

function roleCard(r) {
  if (!r.launched) {
    // A phase with a role file has agents in general but launched none in this run (e.g. nothing to execute).
    const why = r.outcome ? `Outcome: ${r.outcome.replaceAll('_', ' ')}${r.outcome_reason ? ` (${r.outcome_reason})` : ''}.` : null
    return el('div', { class: 'panel', id: `phase-${r.phase}` },
      el('h3', {}, r.phase),
      el('p', { class: 'sub' }, r.role
        ? 'This run launched no agents for this phase.'
        : 'No agents: this phase runs deterministic code and launches no workflow.'),
      why ? el('p', {}, why) : null,
      r.role ? roleNote(r) : null)
  }
  return el('div', { class: 'panel', id: `phase-${r.phase}` },
    el('h3', {}, r.phase, ' ', el('span', { class: 'sub' }, code(r.stage))),
    r.summary ? el('p', {}, r.summary) : el('p', { class: 'sub' }, 'The workflow recorded no summary of this phase.'),
    el('h4', {}, `Agents started (${r.agents.length})`),
    r.agents.length
      ? el('div', {}, r.agents.map(a => el('span', { class: `chip ${a.finished ? 'ok' : 'bad'}`, title: a.finished ? 'returned a result' : 'no result recorded' },
        `${a.label ?? 'unlabelled'}${a.model ? ` · ${a.model}` : ''}`)))
      : el('p', { class: 'sub' }, 'The journal records no agent.'),
    el('h4', {}, 'Settings it was launched with'),
    settingsList(r.settings),
    roleNote(r))
}

// A slower earlier request must not paint over a newer render (main.js only guards the start).
let latest = 0

export async function render(container, { runId }) {
  const mine = ++latest
  try {
    const roles = await runRoles(runId)
    if (mine !== latest) return
    container.replaceChildren(
      el('div', { class: 'panel' },
        el('h2', {}, 'Agents by phase'),
        el('p', { class: 'sub' },
          'Each phase workflow gives each of its agents one prompt and keeps its one result; agents do not message one another, ',
          'and what one phase learns reaches the next through the knowledge base. Settings and agents are this ',
          'run\'s, read from its archive; the role text is the repository\'s file (agents/<phase>.md).')),
      ...roles.map(roleCard))
    // `#run/<id>/agents/<phase>` (the Story links here): bring that phase's card into view and mark it.
    const { focus } = state.get()
    const card = focus ? document.getElementById(`phase-${focus}`) : null
    if (card) {
      card.classList.add('focused')
      card.scrollIntoView({ block: 'start' })
    }
  } catch (err) {
    showError(container, err)
  }
}
