// Overview: the landing view for one run.
//
// Three questions in the order an operator asks them: what this run is and what it cost, how it was
// configured, and — while it is still going — where it has got to. The run is chosen once, globally,
// so this module neither lists nor selects runs, and main.js already draws the phase
// timeline above this container from /runs/<id>/stages.

import { runContext, runMetrics, runningRuns } from '../api.js'
import { el, kv, code, short, showError, fmtDuration, fmtUsd, fmtTokens } from '../dom.js'

const POLL_MS = 7000
const MAX_CONSECUTIVE_ERRORS = 5
const STALLED_MS = 30 * 60 * 1000

const CHIP_CLASS = { passed: 'ok', completed: 'ok', failed: 'bad', killed: 'bad', running: 'warn' }

let timer = null
// Bumped by stop() and so by every render. An in-flight poll compares against it before touching the
// DOM: a response that lands after the view changed is dropped instead of writing into a dead node.
let generation = 0

export function stop() {
  if (timer !== null) clearInterval(timer)
  timer = null
  generation++
}

// Wall-clock gaps here run from seconds to hours; fmtDuration's fixed seconds would read "2731.0s".
const fmtAgo = ms =>
  ms == null ? '—'
    : ms < 60_000 ? `${Math.round(ms / 1000)}s`
    : ms < 3_600_000 ? `${Math.round(ms / 60_000)}m`
    : `${(ms / 3_600_000).toFixed(1)}h`

// The headline figures come from the /runs row, not from /runs/<id>/metrics: RunMetrics carries no
// workflow status, duration or total token count, and the old Runs card read them from it anyway —
// which is why every card showed "unknown", "0.0s", "0".
function headlinePanel(runId, run) {
  const status = run?.status || 'unknown'
  return el('div', { class: 'panel' },
    el('h2', {}, run?.display || runId),
    el('p', { class: 'sub' }, run?.source ? `source: ${run.source}` : 'run'),
    el('span', { class: `chip ${CHIP_CLASS[status] ?? 'warn'}` }, status),
    kv([
      ['Run id', code(runId)],
      ['Cost (CLI reported)', fmtUsd(run?.cost_usd)],
      ['Duration', fmtDuration(run?.duration_ms)],
      ['Tokens', fmtTokens(run?.total_tokens)],
    ]),
  )
}

function contextPanel(context) {
  const c = context
  const rows = [
    ['Started (UTC)', c.started_utc],
    ['Git commit', c.git_commit ? short(c.git_commit) : null],
    ['Working tree', c.git_dirty == null ? null : c.git_dirty ? 'dirty' : 'clean'],
    ['Launcher model', c.launcher_model],
    ['Claude version', c.claude_version],
    ['Discovery config', c.discovery_config],
    ['Discovery config hash', c.config_hash ? short(c.config_hash) : null],
    ['Discovery workflow sha256', c.workflow_sha256 ? short(c.workflow_sha256) : null],
    ['CA config', c.ca_config],
    ['CA config hash', c.ca_config_hash ? short(c.ca_config_hash) : null],
    ['CA workflow sha256', c.ca_workflow_sha256 ? short(c.ca_workflow_sha256) : null],
    ['Discovery timeout', c.discovery_timeout_s == null ? null : `${c.discovery_timeout_s}s`],
    ['CA timeout', c.ca_timeout_s == null ? null : `${c.ca_timeout_s}s`],
    ['Discovery budget', c.discovery_max_budget_usd == null ? null : fmtUsd(c.discovery_max_budget_usd)],
    ['CA budget', c.ca_max_budget_usd == null ? null : fmtUsd(c.ca_max_budget_usd)],
    ['Max concurrent agents', c.discovery_max_concurrent_agents],
  ].filter(([, value]) => value !== null && value !== undefined)

  return el('div', { class: 'panel' },
    el('h2', {}, 'Context'),
    el('p', { class: 'sub' }, 'Recorded when the run started.'),
    rows.length ? kv(rows) : el('p', { class: 'sub' }, 'No recognised fields in this run context.'),
    // Replaces the old card's "View Full Details" modal: the same JSON, without a second DOM layer.
    el('details', {},
      el('summary', {}, 'Full run context'),
      el('pre', {}, JSON.stringify(context, null, 2))),
  )
}

// Only the fields RunMetrics (metrics.py) genuinely computes from persisted events.
function metricsPanel(m) {
  const gates = Object.entries(m.gates ?? {})
  const byAgent = Object.entries(m.agent_calls_by_agent ?? {})
  return el('div', { class: 'panel' },
    el('h2', {}, 'Metrics from recorded events'),
    el('p', { class: 'sub' }, `${m.events} events${m.finished ? '' : ' · no run_finished event recorded'}`),
    kv([
      ['Agent calls', m.agent_calls],
      ['Tool calls', m.tool_calls],
      ['Errors', m.errors ? el('span', { class: 'bad' }, m.errors) : 0],
      ['Input tokens', fmtTokens(m.input_tokens)],
      ['Output tokens', fmtTokens(m.output_tokens)],
      ['Cost (summed from events)', fmtUsd(m.cost_usd)],
      ['Latency total', fmtDuration(m.latency_ms_total)],
      ['Latency median', fmtDuration(m.latency_ms_median)],
      ['Latency max', fmtDuration(m.latency_ms_max)],
      ['Gates', gates.length ? gates.map(([gate, n]) => el('span', { class: 'chip' }, `${gate} ×${n}`)) : '—'],
      ['Calls by agent', byAgent.length ? byAgent.map(([agent, n]) => el('span', { class: 'chip' }, `${agent} ×${n}`)) : '—'],
    ]),
  )
}

const count = n => (n == null ? '—' : String(n))
const chips = obj => {
  const entries = Object.entries(obj ?? {})
  return entries.length ? entries.map(([k, v]) => el('span', { class: 'chip' }, `${k} ×${v}`)) : '—'
}

// A phase-pipeline run's metrics (pipeline_run.pipeline_metrics): counted from its phase journals, tool-call
// audit trails and phase reports. There are no store events, gates or per-call latencies to show here.
function pipelineMetricsPanel(m) {
  const byPhase = Object.entries(m.agents_by_phase ?? {})
  const missingTrail = m.stages_without_tool_trail ?? []
  return el('div', { class: 'panel' },
    el('h2', {}, 'Metrics from the phase archive'),
    el('p', { class: 'sub' },
      `${m.phase_attempts} launched phase attempt${m.phase_attempts === 1 ? '' : 's'}`,
      m.earlier_attempts ? ` (${m.earlier_attempts} superseded by a retry, counted)` : '',
      m.inherited_phases?.length ? ` · excludes ${m.inherited_phases.length} phases copied from the original run` : ''),
    kv([
      ['Agents started', count(m.agents_started)],
      ['Agents that returned a result', m.agents_started != null && m.agents_finished < m.agents_started
        ? el('span', { class: 'warn' }, count(m.agents_finished)) : count(m.agents_finished)],
      ['Agents by phase', byPhase.length
        ? byPhase.map(([phase, n]) => el('span', { class: 'chip' }, `${phase} ${n.finished}/${n.started}`)) : '—'],
      ['Tool calls (agents)', count(m.tool_calls)],
      ['Tool calls with an error', m.tool_errors ? el('span', { class: 'bad' }, m.tool_errors) : count(m.tool_errors)],
      ['Tool calls never answered', count(m.tool_unanswered)],
      ['Tool calls by tool', chips(m.tool_calls_by_tool)],
      ['Cost (CLI reported, summed over phases)', fmtUsd(m.cost_usd)],
      ['Tokens (summed over phase reports)', fmtTokens(m.total_tokens)],
      ['Wall time (summed over phases)', m.wall_s == null ? '—' : `${(m.wall_s / 60).toFixed(1)} min`],
    ]),
    missingTrail.length ? el('p', { class: 'sub' }, `No tool-call audit trail for: ${missingTrail.join(', ')}.`) : null,
  )
}

function liveCard(info) {
  const now = Date.now()
  const started = info.start_time ? Date.parse(info.start_time) : NaN
  const lastEvent = info.last_event_time ? Date.parse(info.last_event_time) : NaN
  const sinceEvent = Number.isNaN(lastEvent) ? null : now - lastEvent
  const hasError = typeof info.last_event === 'string' && info.last_event.toLowerCase().includes('error')
  const [label, statusClass] =
    hasError ? ['Error', 'status-error']
      : sinceEvent !== null && sinceEvent > STALLED_MS ? ['Stalled', 'status-stalled']
      : ['Running', 'status-running pulse-running']
  const percent = Math.max(0, Math.min(100, Number(info.percent_complete) || 0))

  return el('div', { class: 'panel running-card' },
    el('h2', {}, 'Live progress'),
    el('div', {},
      el('span', { class: statusClass }, label),
      el('span', { class: 'sub' }, ` · ${info.type || 'unknown'}`),
      Number.isNaN(started) ? null : el('span', { class: 'sub' }, ` · started ${fmtAgo(now - started)} ago`)),
    el('div', {},
      el('div', { class: 'progress-label' },
        el('span', {}, info.stage || 'unknown'),
        el('span', {}, `${percent}%`)),
      el('div', { class: 'progress-bar' },
        el('div', { class: 'progress-bar-fill', style: `width: ${percent}%` }))),
    kv([
      ['Cost so far', fmtUsd(info.cost_usd_so_far)],
      ['Tokens so far', fmtTokens(info.tokens_so_far)],
      ['Last event', info.last_event || 'pending'],
      ['Last event at', sinceEvent === null ? null : `${fmtAgo(sinceEvent)} ago`],
    ]),
    hasError ? el('div', { class: 'warning-box' }, 'The last recorded event mentions an error.') : null,
  )
}

// /runs/running is the only source of live stage and progress; it reports every run in progress, so
// the entry for this run is picked out by id.
function startLive(runId, node, gen) {
  let consecutiveErrors = 0

  const tick = async () => {
    if (gen !== generation) return
    try {
      const all = await runningRuns()
      if (gen !== generation) return
      consecutiveErrors = 0
      const info = Array.isArray(all) ? all.find(r => r.run_id === runId) : null
      if (!info) {
        stop()
        node.replaceChildren(el('div', { class: 'panel running-empty' },
          el('p', { class: 'sub' }, 'This run is no longer reported as in progress. Reselect it to see its final figures.')))
        return
      }
      node.replaceChildren(liveCard(info))
    } catch (err) {
      if (gen !== generation) return
      consecutiveErrors += 1
      if (consecutiveErrors < MAX_CONSECUTIVE_ERRORS) return
      // Keep polling through the odd failed request, but stop hammering a server that is plainly down.
      stop()
      node.replaceChildren(el('div', { class: 'panel' },
        el('p', { class: 'bad' }, `Live progress paused: ${err.message ?? err}`),
        el('button', { type: 'button', onclick: () => startLive(runId, node, generation) }, 'Retry')))
    }
  }

  node.replaceChildren(el('div', { class: 'panel running-loading' }, el('p', { class: 'sub' }, 'Loading live progress…')))
  timer = setInterval(tick, POLL_MS)
  tick()
}

export async function render(container, { runId, run }) {
  stop()                       // a re-render must never leave the previous timer polling
  const gen = generation
  try {
    // A smoke or store run may have neither context nor events; that is not a failure of this view.
    const [context, metrics] = await Promise.all([
      runContext(runId).catch(() => null),
      runMetrics(runId).catch(() => null),
    ])
    if (gen !== generation) return

    const liveBox = el('div')
    container.replaceChildren(
      headlinePanel(runId, run),
      context ? contextPanel(context) : el('div', { class: 'panel' }, el('p', { class: 'sub' }, 'No run context recorded.')),
      metrics ? (metrics.source === 'phase_archive' ? pipelineMetricsPanel(metrics) : metricsPanel(metrics)) : el('div', { class: 'panel' }, el('p', { class: 'sub' }, 'No events recorded for this run.')),
      liveBox,
    )

    // `finished` comes from the catalog; an unlisted run is treated as possibly live and the first
    // poll settles it.
    if (!run || run.finished === false) startLive(runId, liveBox, gen)
  } catch (err) {
    showError(container, err)
  }
}
