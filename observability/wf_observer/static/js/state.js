// The selected run, and the URL that names it.
//
// Every run view reads the run from here instead of choosing one for itself, so that views
// with separate selections can never show one run's tokens beside another run's evaluation.
//
// The hash is the source of truth for what is on screen, so a run is linkable and survives a
// reload: `#run`, `#run/<id>`, `#run/<id>/<view>`, `#run/<id>/<view>/<focus>` (a section of the view, e.g. a
// phase in Agents).

const listeners = new Set()

const state = {
  tab: 'run',         // the only menu item
  runId: null,
  view: 'overview',   // which run sub-view, when tab === 'run'
  focus: null,        // a section of the view to scroll to, e.g. a phase in Agents
}

export const RUN_VIEWS = ['overview', 'graph', 'flow', 'agents', 'story', 'tokens', 'evaluation']

export function get() {
  return { ...state }
}

export function subscribe(fn) {
  listeners.add(fn)
  return () => listeners.delete(fn)
}

function emit() {
  for (const fn of listeners) fn(get())
}

export function parseHash(hash = location.hash) {
  const parts = hash.replace(/^#/, '').split('/').filter(Boolean)
  if (parts[0] !== 'run') return { tab: 'run', runId: null, view: 'overview', focus: null }
  return {
    tab: 'run',
    runId: parts[1] ? decodeURIComponent(parts[1]) : null,
    view: RUN_VIEWS.includes(parts[2]) ? parts[2] : 'overview',
    focus: RUN_VIEWS.includes(parts[2]) && parts[3] ? decodeURIComponent(parts[3]) : null,
  }
}

export function toHash({ tab, runId, view, focus }) {
  if (tab !== 'run' || !runId) return '#run'
  const tail = view && view !== 'overview' ? `/${view}${focus ? `/${encodeURIComponent(focus)}` : ''}` : ''
  return `#run/${encodeURIComponent(runId)}${tail}`
}

/** Apply a partial change, write it to the hash, and notify subscribers once. */
export function set(patch, { silent = false } = {}) {
  const next = { ...state, ...patch }
  // A focus belongs to the view and run it was given for: moving elsewhere drops it unless the patch sets one.
  if (!('focus' in patch) && (next.view !== state.view || next.runId !== state.runId)) next.focus = null
  const changed = next.tab !== state.tab || next.runId !== state.runId || next.view !== state.view ||
    next.focus !== state.focus
  Object.assign(state, next)
  const hash = toHash(state)
  if (location.hash !== hash) {
    // replaceState, not location.hash: navigating the page's own tabs should not fill the back stack
    // with every click. Explicit back/forward still works through the hashchange listener below.
    history.replaceState(null, '', hash)
  }
  if (changed && !silent) emit()
}

/** Adopt whatever the current URL says, without pushing a new entry. */
export function syncFromHash() {
  set(parseHash(), { silent: false })
}

window.addEventListener('hashchange', () => syncFromHash())
