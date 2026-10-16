// Page shell: one menu item, one run selection, and lazy view modules.
//
// Run describes what happened, under a single selector shared by every sub-view.

import { listRuns, runningRuns, runStages } from './api.js'
import { el, showError, fmtDuration, fmtUsd, fmtTokens } from './dom.js'
import * as state from './state.js'

const sections = {
  run: document.getElementById('run'),
}
const runSelect = document.getElementById('run-select')
const liveButton = document.getElementById('run-live')
const timelineBox = document.getElementById('run-timeline')
const subnav = document.getElementById('run-subnav')
const runView = document.getElementById('run-view')

// Display names only: the stage ids (`ca`, ...) are the run.sh contract and stay as they are.
const STAGE_LABELS = { ca: 'computational-analysis (ca)' }
const VIEW_LABELS = { overview: 'Overview', graph: 'Workflow', flow: 'Phase graph', agents: 'Agents', story: 'Story', tokens: 'Tokens', evaluation: 'Evaluation' }
const loaders = {
  overview: () => import('./run/overview.js'),
  graph: () => import('./run/graph.js'),
  flow: () => import('./run/flow.js'),
  agents: () => import('./run/agents.js'),
  story: () => import('./run/story.js'),
  tokens: () => import('./run/tokens.js'),
  evaluation: () => import('./run/evaluation.js'),
}

let runs = []
let running = []

const runLabel = run => {
  const bits = [run.display || run.id]
  if (run.status && run.status !== 'unknown') bits.push(run.status)
  if (run.total_tokens != null) bits.push(`${fmtTokens(run.total_tokens)} tok`)
  if (run.cost_usd != null) bits.push(fmtUsd(run.cost_usd))
  if (run.duration_ms != null) bits.push(fmtDuration(run.duration_ms))
  if (!run.finished) bits.push('live')
  return bits.join(' · ')
}

function renderSelector() {
  runSelect.replaceChildren(
    ...(runs.length ? [] : [el('option', { value: '' }, 'no runs found')]),
    ...runs.map(run => el('option', { value: run.id }, runLabel(run))),
  )
  const { runId } = state.get()
  if (runId) runSelect.value = runId
}

// A run in progress is not necessarily one run: `scan_running_runs` returns up to 20. The button
// reports how many there are and only jumps straight there when the answer is unambiguous.
function renderLiveButton() {
  const count = running.length
  liveButton.disabled = count === 0
  liveButton.classList.toggle('active', count > 0)
  liveButton.textContent = count === 0 ? 'No run in progress'
    : count === 1 ? `Go to running run: ${running[0].run_id}`
    : `${count} runs in progress…`
  liveButton.title = count > 1 ? running.map(r => r.run_id).join('\n') : ''
}

function chooseRunning() {
  if (running.length === 1) {
    selectRun(running[0].run_id)
    return
  }
  // More than one: let the operator pick rather than guessing which one they meant.
  runView.replaceChildren(el('div', { class: 'panel' },
    el('h2', {}, `${running.length} runs in progress`),
    el('ul', {}, running.map(r => el('li', {},
      el('button', { type: 'button', onclick: () => selectRun(r.run_id) }, r.run_id),
      ` — ${r.stage ?? 'running'}`)))))
}

function selectRun(runId) {
  state.set({ tab: 'run', runId })
}

async function renderTimeline(runId) {
  timelineBox.replaceChildren()
  try {
    const stages = await runStages(runId)
    if (stages.length === 0) return
    timelineBox.replaceChildren(
      el('span', { class: 'timeline-label' }, 'Stages'),
      ...stages.map(s =>
        el('span', { class: `stage stage-${s.status}`, title: s.detail ?? '' }, `${STAGE_LABELS[s.stage] ?? s.stage}: ${s.status}`)))
  } catch {
    // A store or smoke run has no run.sh stages; that is not an error worth a banner.
    timelineBox.replaceChildren()
  }
}

function renderSubnav(active) {
  subnav.replaceChildren(...state.RUN_VIEWS.map(view => el('button', {
    type: 'button',
    role: 'tab',
    'aria-selected': String(view === active),
    onclick: () => state.set({ tab: 'run', view }),
  }, VIEW_LABELS[view])))
}

async function renderRunView(runId, view) {
  renderSubnav(view)
  if (!runId) {
    runView.replaceChildren(el('div', { class: 'panel' }, 'Select a run.'))
    return
  }
  runView.replaceChildren(el('div', { class: 'panel' }, 'Loading…'))
  try {
    const module = await loaders[view]()
    await module.render(runView, { runId, run: runs.find(r => r.id === runId) ?? null })
  } catch (err) {
    showError(runView, err)
  }
}

function renderTabs(tab) {
  for (const [name, section] of Object.entries(sections)) {
    section.hidden = name !== tab
    document.getElementById(`tab-${name}`).setAttribute('aria-selected', String(name === tab))
  }
}

// Renders are async and are triggered by several sources (a state change, the selector, the hash),
// so two can overlap: the second would paint into a container the first is still filling, and the
// view modules' own poll timers would be started twice. Only the newest render may touch the DOM.
let renderGeneration = 0

async function render({ tab, runId, view }) {
  const generation = ++renderGeneration
  const current = () => generation === renderGeneration

  renderTabs(tab)
  renderSelector()
  if (runId) await renderTimeline(runId)
  if (!current()) return
  await renderRunView(runId, view)
}

async function refreshRuns() {
  runs = await listRuns()
  // Default to the newest run rather than leaving the page blank on first load.
  const { tab, runId } = state.get()
  if (tab === 'run' && !runId && runs.length) {
    state.set({ runId: runs[0].id })   // silent when no subscriber yet; a re-render otherwise
    return
  }
  renderSelector()
}

let pollInFlight = false

async function pollRunning() {
  if (pollInFlight || document.hidden) return   // never stack requests; a hidden tab has no one to update
  pollInFlight = true
  try {
    running = await runningRuns()
  } catch {
    running = []
  } finally {
    pollInFlight = false
  }
  renderLiveButton()
}

runSelect.addEventListener('change', () => selectRun(runSelect.value))
liveButton.addEventListener('click', chooseRunning)
for (const name of Object.keys(sections)) {
  document.getElementById(`tab-${name}`).addEventListener('click', () => state.set({ tab: name }))
}

// Boot in one pass: adopt the URL first (so `#run/<id>` is not overwritten by the newest-run
// default), then list the runs, then paint once. Subscribing before this would render twice.
state.set(state.parseHash(), { silent: true })
await refreshRuns()
state.subscribe(render)
await render(state.get())
await pollRunning()
setInterval(pollRunning, 7000)
