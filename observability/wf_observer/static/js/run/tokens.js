// Tokens for the selected run: usage against the configured limits, and what it cost.
//
// The run comes from the page, so this view has no selector and no listing of its own.
// It keeps the two things that are genuinely about tokens: the saved/live source toggle and the
// live poll.
//
// Two cost figures, named. `tokens.py` uses no Anthropic SDK: it reconstructs usage from the CLI's
// own JSONL and prices it from `configs/tokens.yaml`. That estimate disagrees with the figure the
// CLI reports for the same run (by a third or more), so showing one of them labelled "cost"
// would mislead. Both are shown, each said to be what it is.

import { tokensForRun, tokensConfig, listTokenRuns } from '../api.js'
import { el, kv, code, showError, usdLabel, fmtUsd, fmtTokens } from '../dom.js'

const DEFAULT_POLL_MS = 2000

let pollTimer = null
let pollMs = DEFAULT_POLL_MS
// Bumped by every render, so a fetch still in flight for the previous run cannot write into the
// panel that replaced it.
let generation = 0
// The source toggle is remembered while the run stays the same, and re-derived when it changes:
// a run with a snapshot opens on `saved`, a run without one on `live`.
let mode = 'saved'
let modeRunId = null

export function stop() {
  if (pollTimer !== null) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

const percentFill = (used, limit) => (limit ? Math.min(100, (used / limit) * 100) : 0)

function statusClass(used, limit) {
  if (!limit) return 'token-bar-state-ok'
  const pct = (used / limit) * 100
  return pct >= 100 ? 'token-bar-state-bad' : pct >= 80 ? 'token-bar-state-warn' : 'token-bar-state-ok'
}

// `label` is a string, or an array of strings/nodes when it needs markup (e.g. the LIVE badge);
// el() flattens children, so both work unchanged.
function renderTokenBar(label, used, limit) {
  const fill = percentFill(used, limit)
  const pctStr = limit ? `${Math.round(fill)}%` : 'no limit'
  const statusLabel = limit && used > limit ? 'EXCEEDED' : fill >= 100 ? 'AT CAP' : pctStr
  return el('div', { class: 'token-bar-item' },
    el('div', { class: 'token-bar-label' },
      el('span', {}, label),
      el('span', {}, limit ? `${fmtTokens(used)} / ${fmtTokens(limit)}` : fmtTokens(used))),
    el('div', { class: `token-bar-fill ${statusClass(used, limit)}` },
      el('div', { class: 'token-bar-progress', style: limit ? `width: ${fill}%` : 'width: 0%' }),
      el('div', { class: 'token-bar-status' }, statusLabel)),
  )
}

// Nothing measured yet: a run with no usage at all still gets its two bars, so the panel does not
// change shape when the first request lands.
const renderIdleTokenBar = label => el('div', { class: 'token-bar-item' },
  el('div', { class: 'token-bar-label' }, el('span', {}, label), el('span', {}, '—')),
  el('div', { class: 'token-bar-fill token-bar-state-ok' },
    el('div', { class: 'token-bar-progress', style: 'width: 0%' }),
    el('div', { class: 'token-bar-status' }, '—')),
)

function renderBars(data, live) {
  const totals = data.totals
  if (!totals) return el('div', { class: 'token-bar' }, renderIdleTokenBar('Input'), renderIdleTokenBar('Output'))
  const limits = data.limits || {}
  const input = totals.input || { total: 0 }
  return el('div', { class: 'token-bar' },
    renderTokenBar(live ? ['Input ', el('span', { class: 'live-badge' }, 'LIVE')] : 'Input',
      input.total || 0, limits.max_input_tokens),
    renderTokenBar('Output', totals.output || 0, limits.max_output_tokens),
  )
}

const WORKFLOW_WHY = {
  no_agents: 'the workflow started no agents',
  incomplete: 'some agents in the workflow journal have no transcript',
  missing: 'transcript dir not found (deleted with its session): agent usage cannot be recomputed',
  no_launch: 'no workflow launch recorded in this stream (not launched yet, or the launch failed)',
}

function warningChips(data) {
  const chips = []
  if (data.legacy) {
    chips.push(el('div', { class: 'chip warn' },
      'legacy snapshot (pre-v2): token totals are price-weighted and model may be unknown; switch to Live to recompute'))
  }
  for (const w of data.workflows || []) {
    if (w.state === 'ok') continue
    const name = w.run_id ? `${w.workflow_name} ${w.run_id}` : w.stream
    chips.push(el('div', { class: 'chip warn', title: w.transcript_dir || '' }, `${name}: ${WORKFLOW_WHY[w.state] || w.state}`))
  }
  if (data.unpriced_models?.length) {
    chips.push(el('div', { class: 'chip warn' },
      `unpriced models: ${data.unpriced_models.join(', ')} (${data.unpriced_tokens || 0} tokens excluded from USD)`))
  }
  return chips
}

const CLI_COST_HELP = [
  'What the Claude CLI itself reported for this run (summary.json cost_usd; for a phase-pipeline run,',
  'the sum of each phase report\'s session_cost_usd plus the cost of earlier failed attempts).',
  'It is not the estimate above: that one is reconstructed from the CLI transcripts and priced',
  'locally from configs/tokens.yaml, and the two can disagree by a third or more.',
  'Neither figure is a bill.',
].join('\n')

function summaryPanel(data, { live, finished, run }) {
  const totals = data.totals || {}
  const limits = data.limits || {}
  const input = totals.input || {}

  // No reconstruction at all (a phase-pipeline run whose transcripts are gone) is absent, not zero.
  const rows = data.totals ? [
    ['Total input', `${fmtTokens(input.total || 0)} (fresh: ${fmtTokens(input.fresh || 0)}, cache_write: ${fmtTokens(input.cache_write || 0)}, cache_read: ${fmtTokens(input.cache_read || 0)})`],
    ['Total output', fmtTokens(totals.output || 0)],
  ] : [['Reconstructed usage', 'not available: no agent transcript could be read (see the phase table)']]
  if (typeof totals.input_billed_equivalent === 'number') {
    rows.push(['Input (billed-equivalent)', fmtTokens(totals.input_billed_equivalent)])
  }
  // The two figures at the same precision, so the gap between them can be read directly.
  if (data.totals) rows.push([usdLabel('Estimated USD (reconstructed here)', data.pricing), fmtUsd(totals.est_usd)])
  rows.push([
    el('span', { class: 'help', 'data-help': CLI_COST_HELP }, 'Cost USD (reported by the CLI)'),
    // Only from the run row main.js passes in; absent for runs without a summary.json.
    run && run.cost_usd != null ? fmtUsd(run.cost_usd) : 'not reported for this run',
  ])

  rows.push(['Status', limits.exceeded?.length
    ? el('span', { class: 'bad' }, `EXCEEDED: ${limits.exceeded.join(', ')}`)
    : data.status || 'unknown'])
  rows.push(['Source', live && !finished
    ? [el('span', { class: 'token-pulse' }, '●'), ' live · ', data.generated_at ? `updated ${data.generated_at.split('T')[1].split('.')[0]} UTC` : '']
    : live ? 'transcripts (finished run)' : 'saved'])

  const panel = el('div', { class: 'panel' }, el('h2', {}, 'Summary'), renderBars(data, live && !finished), kv(rows))
  const chips = warningChips(data)
  if (chips.length) panel.append(el('div', { style: 'margin-top: 12px' }, ...chips))
  return panel
}

const TH = 'text-align: left; padding: 8px; border-bottom: 1px solid var(--line)'
const THR = 'text-align: right; padding: 8px; border-bottom: 1px solid var(--line)'

function agentsPanel(data) {
  const rows = Object.entries(data.by_agent || {}).map(([id, agent]) => el('tr', {},
    el('td', {}, agent.label || id),
    el('td', {}, agent.role),
    el('td', {}, agent.workflow_run_id ? code(agent.workflow_run_id) : '—'),
    el('td', {}, agent.model),
    el('td', { style: 'text-align: right' }, fmtTokens(agent.input?.total || 0)),
    el('td', { style: 'text-align: right' }, fmtTokens(agent.output || 0)),
    // Three decimals: per-agent estimates are small enough that $0.00 would hide most of them.
    el('td', { style: 'text-align: right' }, agent.est_usd ? `$${agent.est_usd.toFixed(3)}` : '—'),
  ))
  return el('div', { class: 'panel' },
    el('h2', {}, 'Agents'),
    el('table', { style: 'width: 100%; border-collapse: collapse' },
      el('thead', {}, el('tr', {},
        el('th', { style: TH }, 'Label'),
        el('th', { style: TH }, 'Role'),
        el('th', { style: TH }, 'Workflow'),
        el('th', { style: TH }, 'Model'),
        el('th', { style: THR }, 'Input'),
        el('th', { style: THR }, 'Output'),
        el('th', { style: THR }, usdLabel('Est. USD', data.pricing)),
      )),
      el('tbody', {}, rows),
    ),
  )
}

const PHASE_HELP = [
  'Per phase, as each NN-<phase>-report.json records its launch: the CLI session cost (session_cost_usd),',
  'the workflow agents\' tokens (workflow_usage.total_tokens, no input/output split) and the launcher\'s own',
  'tokens (launcher_usage, summed). The two token figures are taken as disjoint, so Total is their sum.',
  '— means the report does not record the figure; it is never read as zero.',
].join('\n')

const td = (text, right = false) => el('td', right ? { style: 'text-align: right' } : {}, text)

// Phase-pipeline runs: the figures their phase reports recorded, one row per phase (plus earlier attempts).
function phasesPanel(data) {
  const rows = []
  for (const p of data.by_phase || []) {
    const name = p.inherited ? `${p.phase} (copied, not re-run)` : p.phase
    rows.push(el('tr', {},
      td(p.launch_stage && p.launch_stage !== p.stage ? `${name} · ${p.launch_stage}` : name),
      td(p.launched ? (p.workflow_status || '—') : 'no launch'),
      td(p.agent_count ?? '—', true),
      td(fmtUsd(p.cost_usd), true),
      td(fmtTokens(p.workflow_tokens), true),
      td(fmtTokens(p.launcher_total), true),
      td(fmtTokens(p.total_tokens), true),
      td(p.wall_s == null ? '—' : `${(p.wall_s / 60).toFixed(1)} min`, true),
    ))
    for (const a of p.earlier_attempts || []) {
      rows.push(el('tr', { class: 'muted' },
        td(`↳ earlier attempt ${a.stage}`), td(a.status || '—'), td('—', true), td(fmtUsd(a.cost_usd), true),
        td(fmtTokens(a.workflow_tokens), true), td('—', true), td(fmtTokens(a.workflow_tokens), true), td('—', true)))
    }
  }
  const t = data.phase_totals || {}
  rows.push(el('tr', { style: 'font-weight: 600' },
    td(data.replay_of ? `Total (this archive: ${data.replay_of} only)` : 'Total'), td(''), td('', true),
    td(fmtUsd(t.cost_usd), true), td(fmtTokens(t.workflow_tokens), true), td(fmtTokens(t.launcher_tokens), true),
    td(fmtTokens(t.total_tokens), true), td(t.wall_s == null ? '—' : `${(t.wall_s / 60).toFixed(1)} min`, true)))
  const head = ['Phase', 'Workflow', 'Agents', 'Cost USD (CLI)', 'Workflow tokens', 'Launcher tokens', 'Total tokens', 'Wall']
  return el('div', { class: 'panel' },
    el('h2', {}, el('span', { class: 'help', 'data-help': PHASE_HELP }, 'Phases (from the phase reports)')),
    el('table', { style: 'width: 100%; border-collapse: collapse' },
      el('thead', {}, el('tr', {}, ...head.map((h, i) => el('th', { style: i < 2 ? TH : THR }, h)))),
      el('tbody', {}, rows)))
}

/** The `/tokens` row for this run: which sources it has, and whether it is still going. */
async function runFlags(runId) {
  try {
    // A lookup, not a listing: `/tokens` is the only endpoint that reports has_saved/has_live, and
    // nothing of this list reaches the screen.
    const row = (await listTokenRuns()).find(r => r.run_id === runId)
    return { hasSaved: Boolean(row?.has_saved), finished: Boolean(row?.finished) }
  } catch {
    // The toggle still works; only the default and the poll's stop condition are guessed.
    return { hasSaved: true, finished: false }
  }
}

export async function render(container, { runId, run }) {
  // Defensive: a re-render must never leave the previous timer running.
  stop()
  const mine = ++generation

  const flags = await runFlags(runId)
  try {
    pollMs = ((await tokensConfig()).poll_s ?? 2) * 1000
  } catch {
    pollMs = DEFAULT_POLL_MS           // config unreadable: the default interval is still usable
  }
  if (mine !== generation) return

  if (runId !== modeRunId) {
    modeRunId = runId
    mode = flags.hasSaved ? 'saved' : 'live'
  }

  const detail = el('div')

  const modeButton = (value, label) => el('button', {
    type: 'button',
    'aria-selected': String(mode === value),
    onclick: () => {
      if (mode === value) return
      mode = value
      // Re-entrant by design; render() stops the poller first. Nothing awaits this click handler,
      // so its failure has to be shown here rather than escaping as an unhandled rejection.
      render(container, { runId, run }).catch(err => showError(container, err))
    },
  }, label)

  const controls = el('div', { class: 'panel' },
    el('div', { class: 'token-run-selector' },
      el('span', {}, 'Source:'),
      el('div', { class: 'token-mode-toggle' }, modeButton('saved', 'Saved'), modeButton('live', 'Live')),
    ),
  )
  container.replaceChildren(controls, detail)

  async function load() {
    let data
    try {
      data = await tokensForRun(runId, mode)
    } catch (err) {
      stop()
      if (mine === generation && detail.isConnected) showError(detail, err)
      return
    }
    // A later render already owns the page, or ours was replaced while the request was in flight.
    if (mine !== generation || !detail.isConnected) {
      stop()
      return
    }
    if (data.source === 'phase_reports') {
      detail.replaceChildren(phasesPanel(data))
      return
    }
    detail.replaceChildren(summaryPanel(data, { live: mode === 'live', finished: flags.finished, run }), agentsPanel(data),
      ...(data.by_phase ? [phasesPanel(data)] : []))
  }

  await load()
  if (mine !== generation) return

  // Saved is a snapshot: nothing to poll. A finished run is a snapshot too, even read live.
  if (mode !== 'live' || flags.finished) return
  pollTimer = setInterval(() => {
    // The run tab may be hidden (the shell hides the section, the browser hides the page): skip the
    // request but keep the timer, so the panel is current again as soon as it is visible.
    if (document.hidden || detail.closest('[hidden]')) return
    if (mine !== generation || !detail.isConnected) { stop(); return }
    load()
  }, pollMs)
}
