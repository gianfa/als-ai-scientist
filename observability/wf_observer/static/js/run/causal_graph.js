// Renders the causal-graph agent's output (nodes + edges) as SVG in a popup dialog.
//
// Encoding follows the agent's schema: causal edges are directed solid arrows, associational edges
// are undashed-bidirectional in meaning and drawn dashed without a head, because no direction is
// asserted. Edge sign is the colour (increases / decreases / unknown). Hover an edge for its
// evidence IDs and assumptions. Layout is a plain circle: deterministic and dependency-free.

import { el } from '../dom.js'

const NS = 'http://www.w3.org/2000/svg'
const svg = (tag, attrs = {}, ...children) => {
  const e = document.createElementNS(NS, tag)
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v)
  e.append(...children)
  return e
}

const SIGN_COLOR = { increases: '#2f6b3a', decreases: '#a33a2a', unknown: '#8a8a84' }
const W = 720
const H = 520
const R = 34 // node radius

function layout(nodes) {
  const cx = W / 2
  const cy = H / 2
  const rx = W / 2 - R - 70
  const ry = H / 2 - R - 30
  return new Map(nodes.map((n, i) => {
    const a = (2 * Math.PI * i) / Math.max(nodes.length, 1) - Math.PI / 2
    return [n.id, { x: cx + rx * Math.cos(a), y: cy + ry * Math.sin(a) }]
  }))
}

function edgeTitle(edge) {
  const lines = [`${edge.id}: ${edge.source} → ${edge.target} (${edge.relation}, ${edge.sign})`,
    `evidence: ${(edge.evidence_ids ?? []).join(', ') || '—'}`]
  for (const a of edge.assumptions ?? []) lines.push(`assumption: ${a}`)
  return lines.join('\n')
}

export function renderCausalGraph(graph) {
  const pos = layout(graph.nodes)
  const root = svg('svg', { viewBox: `0 0 ${W} ${H}`, width: '100%', role: 'img', 'aria-label': 'Causal graph' })
  const defs = svg('defs')
  for (const [sign, color] of Object.entries(SIGN_COLOR)) {
    defs.append(svg('marker', { id: `arrow-${sign}`, viewBox: '0 0 10 10', refX: '9', refY: '5', markerWidth: '7', markerHeight: '7', orient: 'auto-start-reverse' },
      svg('path', { d: 'M0,0 L10,5 L0,10 z', fill: color })))
  }
  root.append(defs)

  for (const edge of graph.edges) {
    const a = pos.get(edge.source)
    const b = pos.get(edge.target)
    if (!a || !b) continue // dangling edge: skipped rather than drawn to a made-up place
    const dx = b.x - a.x
    const dy = b.y - a.y
    const len = Math.hypot(dx, dy) || 1
    const causal = edge.relation === 'causal'
    const color = SIGN_COLOR[edge.sign] ?? SIGN_COLOR.unknown
    const line = svg('line', {
      x1: a.x + (dx / len) * R, y1: a.y + (dy / len) * R,
      x2: b.x - (dx / len) * (R + 2), y2: b.y - (dy / len) * (R + 2),
      stroke: color, 'stroke-width': '2.2', 'stroke-dasharray': causal ? '' : '6 4',
      ...(causal ? { 'marker-end': `url(#arrow-${edge.sign in SIGN_COLOR ? edge.sign : 'unknown'})` } : {}),
    }, svg('title', {}, edgeTitle(edge)))
    // A wide transparent twin makes the thin line easy to hover.
    const hit = svg('line', { x1: line.getAttribute('x1'), y1: line.getAttribute('y1'), x2: line.getAttribute('x2'), y2: line.getAttribute('y2'), stroke: 'transparent', 'stroke-width': '12' },
      svg('title', {}, edgeTitle(edge)))
    root.append(line, hit)
  }

  for (const node of graph.nodes) {
    const p = pos.get(node.id)
    const isOutcome = node.id === graph.outcome_node_id
    root.append(svg('g', {},
      svg('circle', { cx: p.x, cy: p.y, r: R, fill: 'var(--chip)', stroke: isOutcome ? 'var(--bad)' : 'var(--accent)', 'stroke-width': isOutcome ? '3' : '1.5' }),
      svg('text', { x: p.x, y: p.y - 2, 'text-anchor': 'middle', 'font-size': '11', fill: 'var(--text)' }, truncate(node.label, 14)),
      svg('text', { x: p.x, y: p.y + 12, 'text-anchor': 'middle', 'font-size': '9', fill: 'var(--muted)' }, node.kind ?? ''),
      svg('title', {}, `${node.id}: ${node.label} (${node.kind})${isOutcome ? ' — outcome' : ''}`)))
  }
  return root
}

const truncate = (s, n) => (String(s).length > n ? `${String(s).slice(0, n - 1)}…` : String(s))

function legend() {
  return el('p', { class: 'muted' },
    'Solid arrow = causal (directed). Dashed line = associational (no direction asserted). ',
    'Green = increases, red = decreases, grey = unknown sign. Red ring = outcome node. Hover an edge for evidence and assumptions.')
}

export function causalGraphButton(graph) {
  const dialog = el('dialog', { class: 'causal-dialog' },
    el('h3', {}, `Causal graph (${graph.nodes.length} nodes, ${graph.edges.length} edges)`),
    graph.nodes.length ? renderCausalGraph(graph) : el('p', {}, 'The graph is empty.'),
    legend(),
    el('button', { type: 'button', onclick: () => dialog.close() }, 'Close'))
  dialog.addEventListener('click', e => { if (e.target === dialog) dialog.close() }) // backdrop click
  return el('span', {},
    el('button', { type: 'button', onclick: () => dialog.showModal() }, 'View causal graph'),
    dialog)
}
