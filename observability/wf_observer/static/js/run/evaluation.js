// Evaluation of one run. A run of the phase pipeline shows its scorecard (evaluation/scorecard.py), computed by
// the server from the run's archive: one row per evaluation dimension, `no scorer` where no honest number can be
// computed, and no overall score. A run of the previous architecture shows the structural checks and metrics
// recorded in its evaluation.json.
//
// The run is chosen globally, so this view has no run dropdown of its own: it shows per-check
// pass/fail/warn and the metrics table.

import { evaluationForRun } from '../api.js'
import { el, kv, showError } from '../dom.js'

const CELL = 'padding: 8px; border-bottom: 1px solid var(--line)'
const HEAD = `text-align: left; ${CELL}; font-weight: 600`

// PASS is the only unambiguous success; anything unrecognised reads as a failure rather than as a
// quiet pass.
const statusClass = status =>
  status === 'PASS' ? 'ok'
    : status === 'WARN' || status === 'PASS_WITH_WARNINGS' ? 'warn'
    : 'bad'

const table = (headers, rows) => el('table', {},
  el('thead', {}, el('tr', {}, headers.map(h => el('th', { style: HEAD }, h)))),
  el('tbody', {}, rows))

const cell = child => el('td', { style: CELL }, child ?? '—')

function statusPanel(runId, data) {
  const status = data.structural_status || 'UNKNOWN'
  const summary = data.summary || {}
  return el('div', { class: 'panel' },
    el('h2', {}, 'Evaluation Status'),
    el('p', {}, el('span', { class: statusClass(status) }, status)),
    kv([
      ['Run', runId],
      ['Evaluated at', data.timestamp],
      ['Failed stage', data.failed_stage],
      ['Passed', summary.passed ?? 0],
      ['Failed', summary.failed ?? 0],
      ['Warnings', summary.warnings ?? 0],
      ['Not Evaluable', summary.not_evaluable ?? 0],
    ]),
  )
}

function checksPanel(checks) {
  const rows = checks.map(check => {
    const status = check.status || 'UNKNOWN'
    return el('tr', {},
      cell(check.name),
      cell(el('span', { class: statusClass(status) }, status)),
      cell(check.evidence),
      cell(check.detail))
  })
  return el('div', { class: 'panel' },
    el('h2', {}, 'Checks'),
    table(['Name', 'Status', 'Evidence', 'Detail'], rows))
}

function metricsPanel(metrics) {
  const rows = metrics.map(metric => {
    const value = metric.value
    const display = value === 'NOT_EVALUABLE'
      ? el('span', { class: 'warn' }, value)
      : value !== null && typeof value === 'object' ? JSON.stringify(value)
      : value
    return el('tr', {},
      cell(metric.name),
      cell(display),
      cell(metric.method),
      cell(metric.deterministic ? 'yes' : 'no'))
  })
  return el('div', { class: 'panel' },
    el('h2', {}, 'Metrics'),
    table(['Name', 'Value', 'Method', 'Deterministic'], rows))
}

// A value can be a number, a string, or a record of several measurements: shown as compact JSON, never dropped.
const showValue = value =>
  value === null || value === undefined ? '—'
    : typeof value === 'object' ? JSON.stringify(value)
    : String(value)

const scoreClass = status => status === 'scored' ? 'ok' : status === 'null' ? 'warn' : 'sub'

function scorecardPanels(runId, data) {
  const counts = data.counts || {}
  const rows = (Array.isArray(data.rows) ? data.rows : []).map(row => el('tr', {},
    cell(row.id),
    cell(row.dimension),
    cell(showValue(row.value)),
    cell(el('span', { class: scoreClass(row.status) }, row.status)),
    cell(row.from),
    cell(row.limit)))
  return [
    el('div', { class: 'panel' },
      el('h2', {}, 'Scorecard'),
      el('p', { class: 'sub' }, 'One row per evaluation dimension. `no scorer`: no honest number can be computed '
        + 'from a run archive, and the reason is given. `null`: a scorer exists but its input is missing. '
        + 'There is no overall score.'),
      kv([
        ['Run', data.run_id ?? runId],
        ['Knowledge base', data.kb],
        ['Scored', counts.scored ?? 0],
        ['Missing input', counts.null ?? 0],
        ['No scorer', counts['no scorer'] ?? 0],
      ])),
    el('div', { class: 'panel' },
      table(['Id', 'Dimension', 'Value', 'Status', 'Read from', 'Limit'], rows)),
  ]
}

export async function render(container, { runId }) {
  let data
  try {
    data = await evaluationForRun(runId)
  } catch (err) {
    // A run without an evaluation stage is ordinary, not an error. getJSON folds the HTTP status
    // into the message, so the server's 404 body ("evaluation not found") is what identifies it.
    const message = String(err.message ?? err)
    if (/not found|404/i.test(message)) {
      container.replaceChildren(el('div', { class: 'panel' },
        el('p', { class: 'sub' }, `No evaluation recorded for ${runId}.`)))
    } else {
      showError(container, err)
    }
    return
  }

  if (data.kind === 'scorecard') {
    container.replaceChildren(...scorecardPanels(runId, data))
    return
  }

  const checks = Array.isArray(data.checks) ? data.checks : []
  const metrics = data.scientific_metrics && typeof data.scientific_metrics === 'object'
    ? Object.values(data.scientific_metrics)
    : []

  container.replaceChildren(
    statusPanel(runId, data),
    ...(checks.length ? [checksPanel(checks)] : []),
    ...(metrics.length ? [metricsPanel(metrics)] : []),
  )
}
