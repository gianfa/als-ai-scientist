// DOM helpers shared by every view.
//
// All text goes through textContent: run fields (e.g. agent prompts) are never parsed as HTML.

export const el = (tag, attrs = {}, ...children) => {
  const e = document.createElement(tag)
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') {
      e.className = v
    } else if (k.startsWith('on')) {
      e[k] = v                 // event handler: a property, not an attribute
    } else {
      e.setAttribute(k, v)
    }
  }
  for (const c of children.flat()) if (c !== null && c !== undefined) e.append(c instanceof Node ? c : String(c))
  return e
}

export const kv = pairs => el('dl', { class: 'kv' }, pairs.flatMap(([k, v]) => [el('dt', {}, k), el('dd', {}, v ?? '—')]))
export const code = s => el('code', {}, s ?? '—')
export const short = h => (h ? code(h.slice(0, 12)) : '—')

export function showError(target, err) {
  target.replaceChildren(el('div', { class: 'panel error' }, String(err.message ?? err)))
}

// Hover explanation of the USD estimate, built from the pricing basis the server actually applied.
export function usdHelp(pricing) {
  const m = pricing?.multipliers
  const lines = ['Estimated cost (not a bill), summed per API request:']
  if (m) {
    lines.push(
      `input_eq = fresh + ${m.cache_write_5m}·cache_write_5m + ${m.cache_write_1h}·cache_write_1h + ${m.cache_read}·cache_read`,
      'USD = (input_eq · input_price + output · output_price) / 1e6  (prices in $/MTok)',
      `Only ${pricing.priced_speed}-speed requests of models in configs/tokens.yaml are priced; the rest (e.g. fast mode) is reported as unpriced and excluded.`,
      `Prices: ${pricing.source ?? 'source not recorded'}`,
    )
  } else {
    lines.push('This response does not record its pricing basis: it is a snapshot saved before schema v3, or the observer server predates it (restart it). Switch to Live on a restarted server to see the formula.')
  }
  return lines.join('\n')
}

export const usdLabel = (text, pricing) => el('span', { class: 'help', 'data-help': usdHelp(pricing) }, text)

// One body-level tooltip for every [data-help] element. Native `title` tooltips never show on panels that
// polling re-renders every few seconds (the hovered node is replaced before the browser's delay), and a CSS
// ::after tooltip would be clipped by `.panel { overflow-x: auto }`. Delegation survives re-renders.
const helpTip = el('div', { id: 'help-tip', hidden: '' })
document.body.append(helpTip)
document.addEventListener('mousemove', e => {
  const target = e.target instanceof Element ? e.target.closest('[data-help]') : null
  if (!target) { helpTip.hidden = true; return }
  helpTip.textContent = target.dataset.help
  helpTip.hidden = false
  const x = Math.min(e.clientX + 12, window.innerWidth - helpTip.offsetWidth - 8)
  const y = e.clientY + 16 + helpTip.offsetHeight > window.innerHeight ? e.clientY - helpTip.offsetHeight - 8 : e.clientY + 16
  helpTip.style.left = `${Math.max(8, x)}px`
  helpTip.style.top = `${Math.max(8, y)}px`
})
document.addEventListener('scroll', () => { helpTip.hidden = true }, true)

export const fmtDuration = ms => (ms == null ? '—' : `${(ms / 1000).toFixed(1)}s`)
export const fmtUsd = usd => (usd == null ? '—' : `$${Number(usd).toFixed(2)}`)
export const fmtTokens = n => (n == null ? '—' : Number(n).toLocaleString())
