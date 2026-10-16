// The server's read-only HTTP surface, in one place.
//
// Every run-scoped endpoint takes the same run id, because the server resolves them all through one
// RunCatalog.

export async function getJSON(url) {
  const r = await fetch(url)
  const body = await r.json()
  if (!r.ok) throw new Error(`${url}: ${body.error ?? r.status}${body.detail ? '\n' + body.detail : ''}`)
  return body
}

export const listRuns = () => getJSON('/runs')
export const runningRuns = () => getJSON('/runs/running')
export const runContext = id => getJSON(`/runs/${encodeURIComponent(id)}`)
export const runStages = id => getJSON(`/runs/${encodeURIComponent(id)}/stages`)
export const runEvents = id => getJSON(`/runs/${encodeURIComponent(id)}/events`)
export const runStory = id => getJSON(`/runs/${encodeURIComponent(id)}/story`)
export const runPhases = id => getJSON(`/runs/${encodeURIComponent(id)}/phases`)
export const runRoles = id => getJSON(`/runs/${encodeURIComponent(id)}/roles`)
export const runMetrics = id => getJSON(`/runs/${encodeURIComponent(id)}/metrics`)

export const listTokenRuns = () => getJSON('/tokens')
export const tokensConfig = () => getJSON('/tokens-config')
export const tokensForRun = (id, mode) => getJSON(`/tokens/${encodeURIComponent(id)}?mode=${mode}`)

export const evaluationForRun = id => getJSON(`/evaluation/${encodeURIComponent(id)}`)
