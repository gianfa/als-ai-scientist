// Phase graph: the workflow this run executed, as a chain of phases, drawn from `GET /runs/<id>/phases`.
//
// Plain HTML/CSS, no diagram library: the UI is served offline (Chart.js is vendored), Mermaid would come
// from a CDN, and the graph is a sequence, so it needs no layout engine. Every figure is read from the
// archive by the server; `—` means the archive does not record it, not zero.

import { runPhases } from '../api.js'
import { el, showError, fmtUsd } from '../dom.js'

const STATUS_CLASS = { completed: 'ok', passed: 'ok', failed: 'bad', running: 'warn', not_reached: 'idle', not_marked_completed: 'warn' }
const ATTEMPT_CLASS = { completed: 'ok', failed: 'bad', stopped: 'bad', killed: 'bad' }

const fmtWall = s => (s == null ? '—' : s < 90 ? `${s.toFixed(0)} s` : `${(s / 60).toFixed(1)} min`)
const label = s => String(s).replaceAll('_', ' ')

function attemptRow(a, last) {
  const agents = `${a.agents_finished}/${a.agents_started} agents returned`
  const tools = a.tool_calls == null ? 'tool calls not recorded' : `${a.tool_calls} tool calls`
  return el('li', {},
    el('code', {}, a.stage), ' ',
    el('span', { class: `chip ${ATTEMPT_CLASS[a.status] ?? 'warn'}` }, a.status ?? 'status not recorded'),
    ` ${agents} · ${tools} · ${fmtUsd(a.cost_usd)}`,
    last ? null : el('span', { class: 'sub' }, ' (superseded)'))
}

function phaseCard(p) {
  const agents = p.agents_started == null ? null
    : `${p.agents_finished}/${p.agents_started} agents returned a result`
  const outcome = p.outcome ? `${label(p.outcome)}${p.outcome_reason ? `: ${p.outcome_reason}` : ''}` : null
  return el('div', { class: `flow-node flow-${p.status}`, title: p.detail ?? '' },
    el('div', { class: 'flow-head' },
      el('strong', {}, p.phase),
      el('span', { class: `chip ${STATUS_CLASS[p.status] ?? 'warn'}` }, label(p.status))),
    p.stage ? el('div', { class: 'sub' }, el('code', {}, p.stage)) : null,
    p.inherited ? el('div', { class: 'warn' }, 'copied from the original run, not re-run here') : null,
    p.planned ? null : el('div', { class: 'sub' }, 'not in the plan this run started with'),
    p.launched
      ? el('dl', { class: 'kv' },
        el('dt', {}, 'Agents'), el('dd', {}, agents ?? '—'),
        el('dt', {}, 'Tool calls'), el('dd', {}, p.tool_calls == null ? '—' : String(p.tool_calls)),
        el('dt', {}, 'Cost'), el('dd', {}, fmtUsd(p.cost_usd)),
        el('dt', {}, 'Wall'), el('dd', {}, fmtWall(p.wall_s)),
        el('dt', {}, 'Workflow'), el('dd', {}, p.workflow_status ?? '—'))
      : el('div', { class: 'sub' }, p.stage ? 'No agents: this phase launches no workflow.' : 'No report in the archive.'),
    outcome ? el('div', {}, el('span', { class: 'sub' }, 'Outcome: '), outcome) : null,
    p.attempts.length > 1
      ? el('details', {}, el('summary', {}, `${p.attempts.length} attempts`),
        el('ul', { class: 'flow-attempts' }, p.attempts.map((a, i) => attemptRow(a, i === p.attempts.length - 1))))
      : null,
    p.agent_labels.length
      ? el('details', {}, el('summary', {}, 'Agents launched'),
        el('div', {}, p.agent_labels.map(l => el('span', { class: 'chip' }, l ?? 'unlabelled'))))
      : null)
}

// A slower earlier request must not paint over a newer render (main.js only guards the start).
let latest = 0

export async function render(container, { runId }) {
  const mine = ++latest
  try {
    const { phases, edges, replay_of: replayOf } = await runPhases(runId)
    if (mine !== latest) return
    const byFrom = new Map(edges.map(e => [e.from, e.to]))
    const nodes = phases.flatMap((p, i) => [
      i > 0 && byFrom.get(phases[i - 1].phase) === p.phase ? el('div', { class: 'flow-edge', 'aria-hidden': 'true' }, '→') : null,
      phaseCard(p),
    ])
    container.replaceChildren(el('div', { class: 'panel' },
      el('h2', {}, 'Executed phases'),
      el('p', { class: 'sub' },
        'Phases in the order the run executed them, read from its phase reports, journals and state.json. ',
        'The pipeline runs one phase after another; an arrow means "followed by", not a data dependency. ',
        replayOf ? `This is a replay archive: only ${replayOf} was re-run.` : ''),
      el('div', { class: 'flow' }, nodes)))
  } catch (err) {
    showError(container, err)
  }
}
