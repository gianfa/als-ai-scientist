// What happened in a run: a deterministic summary, then the recorded exchanges, one per agent.
//
// Agents never message each other: the workflow script prompts each agent and receives its reply
// (see story.py). On a phase-pipeline run each exchange also lists the agent's tool calls in order.
// Prompts are timestamped when recorded; replies are not, and are shown as such.

import { runStory } from '../api.js'
import { el, showError } from '../dom.js'

const time = ts => (ts ? ts.replace('T', ' ').replace(/\.\d+Z$/, 'Z') : 'time not recorded')

function verdictOf(reply) {
  try { return JSON.parse(reply)?.verdict ?? null } catch { return null }
}

function block(title, text, truncated) {
  return text ? el('details', {}, el('summary', {}, truncated ? `${title} (truncated)` : title), el('pre', {}, text)) : null
}

// Chat: the workflow's prompt on the left, the agent's reply on the right, in prompt order.
function bubble(side, who, when, verdict, ...body) {
  return el('div', { class: `bubble bubble-${side}` },
    el('div', { class: 'bubble-head' }, el('strong', {}, who), ' ', el('time', {}, when),
      verdict ? el('span', { class: `chip verdict verdict-${verdict}` }, verdict) : null),
    ...body)
}

// A neutral event between messages: what the workflow did with them. Details expand on demand.
function noteRow(n) {
  const head = el('span', {}, n.headline)
  return el('div', { class: `note note-${n.tone}` },
    n.details.length
      ? el('details', {}, el('summary', {}, head), el('pre', {}, n.details.join('\n')))
      : head)
}

const shortTool = t => String(t ?? '?').replace(/^mcp__/, '')
const clock = ts => (ts ? ts.replace(/^.*T/, '').replace(/\.\d+Z$/, '') : '—')

// The agent's own activity between its prompt and its reply: its tool calls in order, as the audit trail
// recorded them (inputs and results already truncated there). None = no trail archived; [] = no call.
function toolsRow(e) {
  if (e.tools === undefined) return null
  if (e.tools === null) return el('div', { class: 'note' }, el('span', {}, 'tool calls not recorded for this phase'))
  if (e.tools.length === 0) return el('div', { class: 'note' }, el('span', {}, 'no tool calls: answered from its prompt alone'))
  const counts = new Map()
  for (const t of e.tools) counts.set(shortTool(t.tool), (counts.get(shortTool(t.tool)) ?? 0) + 1)
  const errors = e.tools.filter(t => t.status !== 'ok').length
  const head = `${e.tools.length} tool call${e.tools.length === 1 ? '' : 's'}` + (errors ? ` (${errors} not ok)` : '') + ': ' +
    [...counts].map(([t, n]) => (n > 1 ? `${t} ×${n}` : t)).join(', ')
  return el('div', { class: 'tools' },
    el('details', {}, el('summary', {}, head),
      el('ol', {}, e.tools.map(t => el('li', { class: t.status === 'ok' ? '' : 'bad' },
        el('code', {}, shortTool(t.tool)), ` ${clock(t.at)} ${t.status ?? ''}`,
        el('div', { class: 'tool-io' }, el('span', { class: 'sub' }, 'in: '), t.input_preview ?? '—'),
        el('div', { class: 'tool-io' }, el('span', { class: 'sub' }, `out${t.result_bytes != null ? ` (${t.result_bytes} B)` : ''}: `), t.result_preview ?? '—'))))))
}

// The agent's phase, linked to that phase's card in the Agents view of the same run.
function phaseLink(runId, e, text) {
  if (!e.phase || !runId) return text
  return el('a', { href: `#run/${encodeURIComponent(runId)}/agents/${encodeURIComponent(e.phase)}`,
    title: `The ${e.phase} role in the Agents view` }, text)
}

function exchangeRows(e, runId) {
  const sender = e.from ?? 'unknown sender'
  // A phase-pipeline exchange names its stage, i.e. which phase attempt's workflow it belongs to.
  const agent = (e.to ?? 'unknown agent') + (e.stage ? ` · ${e.stage}` : '')
  // The same name with its stage (or phase) linked to the role, for the bubble heads.
  const where = e.stage ?? e.phase
  const head = prefix => el('span', {}, prefix, e.to ?? 'unknown agent', ...(where ? [' · ', phaseLink(runId, e, where)] : []))
  return [
    e.prompt ? bubble('left', head(`${sender} → `), time(e.timestamp), null, block('Prompt', e.prompt, e.prompt_truncated))
      : e.tools !== undefined ? el('div', { class: 'note' }, el('span', {}, `${agent}: prompt not archived (no transcript for this run)`)) : null,
    toolsRow(e),
    e.reply ? bubble('right', head(''), 'time not recorded', e.verdict, block('Reply', e.reply, e.reply_truncated))
      : e.tools !== undefined ? el('div', { class: 'note note-warn' }, el('span', {}, `${agent}: no result recorded`)) : null,
    ...(e.notes ?? []).map(noteRow),
  ]
}

const PIPELINE_NOTE = 'In the phase pipeline agents do not talk to each other. Each phase workflow gives each agent one ' +
  'prompt (left) and keeps its one structured result (right); in between, the agent works alone through its tool ' +
  'calls, listed in order. What one agent learns from another reaches it only through the knowledge base: records ' +
  'packed into its prompt, or read with the kb_* tools.'


// A slower earlier request must not paint over a newer render (main.js only guards the start).
let latest = 0

export async function render(container, { runId }) {
  const mine = ++latest
  try {
    const { kind, narrative, exchanges, closing } = await runStory(runId)
    if (mine !== latest) return
    if (exchanges.length === 0 && narrative.length === 0) {
      container.replaceChildren(el('div', { class: 'panel' }, 'This run recorded no agent prompts or replies.'))
      return
    }
    container.replaceChildren(
      el('div', { class: 'panel' }, el('h2', {}, 'What happened'),
        ...narrative.map(l => el('p', {}, l)),
        el('p', { class: 'muted' }, 'Generated from the recorded journals by fixed templates; no model wrote this.')),
      el('div', { class: 'panel' }, el('h2', {}, kind === 'phase_pipeline' ? 'Agents: prompt, tool calls, result' : 'Agent conversation'),
        kind === 'phase_pipeline' ? el('p', { class: 'sub' }, PIPELINE_NOTE) : null,
        ...exchanges.flatMap(e => exchangeRows(e, runId)), ...(closing ?? []).map(noteRow)))
  } catch (err) {
    showError(container, err)
  }
}
