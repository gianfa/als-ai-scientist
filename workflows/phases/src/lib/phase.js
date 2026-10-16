// Shared contract of the phase workflows.
//
// A phase workflow is a pure function: injected args in, proposals out. It never sees IDs it must invent or
// statuses it could set; the orchestrator (Python) validates and writes every proposal. Every phase returns
//   { phase, outputs: [{ agent_id, output }], dropped: [agent_id] }
// where `dropped` lists the agents that returned nothing (skipped, died, or failed their schema), so a cap or a
// failure is never silent.

export function requirePhaseArgs(a, phase, keys) {
  if (!a || typeof a !== 'object') throw new Error(`${phase}: args must be an object`)
  if (a.phase !== phase) throw new Error(`${phase}: args.phase is ${a.phase}`)
  for (const key of ['run_id', 'objective', 'schema', ...keys]) {
    if (a[key] === undefined || a[key] === null) throw new Error(`${phase}: args.${key} is missing`)
  }
  if (typeof a.schema !== 'object' || a.schema.type !== 'object') throw new Error(`${phase}: args.schema must be an object schema`)
  return a
}

export function agentOptions(a, label) {
  const opts = { label, phase: a.phase, schema: a.schema }
  if (a.model) opts.model = a.model
  if (a.effort) opts.effort = a.effort
  return opts
}

export function collect(phase, agentIds, results) {
  const outputs = []
  const dropped = []
  results.forEach((output, i) => {
    if (output) outputs.push({ agent_id: agentIds[i], output })
    else dropped.push(agentIds[i])
  })
  if (dropped.length) log(`${phase}: ${dropped.length} agent(s) returned nothing: ${dropped.join(', ')}`)
  return { phase, outputs, dropped }
}

// An empty slice means no records were packed into this prompt; it does not mean the store is empty. The
// `literature` phase is handed none on purpose (pipeline/orchestrator.py: a clean room, so extraction is not
// steered by what is already recorded). An agent told the store is empty has no reason to check it, so the
// sentence says what is true of the prompt and points at the KB tools.
export function contextBlock(items) {
  if (!items || items.length === 0) {
    return 'No knowledge-base records are packed into this prompt. That does not mean the knowledge base is ' +
      'empty: it spans this run and every earlier one, and the read-only `mcp__kb__*` tools described above are ' +
      'the only way you reach it here. Search it before asserting something it may already hold.'
  }
  return 'Knowledge-base records you may cite by ID (claims carry a computed epistemic_status you must not change):\n' +
    JSON.stringify(items, null, 1)
}

// The knowledge base is shared across runs and holds far more than the slice packed into the prompt. These
// read-only tools let an agent resolve an ID it meets and look past the slice, so later runs build on earlier
// ones instead of restating them. The launcher grants the tools to every phase (pipeline/launcher.py); `verify`
// is the one phase that does not mention them, because its verdict must rest on the quote it is given.
export function kbBlock() {
  return 'The knowledge base spans this run and every earlier one, and holds more than the records quoted below. ' +
    'Four read-only tools reach it: `mcp__kb__kb_get` (records by ID), `mcp__kb__kb_neighbourhood` (a record and ' +
    'its links, `hops` deep), `mcp__kb__kb_search` (keyword; it searches the evidence, claims and entities, so ' +
    'to reach a record this system wrote about itself — a hypothesis, a declared gap, a proposed experiment, a ' +
    'decision, an intervention — pass `types`, for example `types: ["Hypothesis", "Experiment"]`) ' +
    'and `mcp__kb__kb_mechanism_graph` (the causal edges ' +
    'built up by this run and every earlier one: cause, effect, mechanism and the status computed from ' +
    'verified relations; it is a graph of asserted claims, not a causal model identified from data, so an ' +
    '`unresolved` edge is a hypothesis to test). `mcp__kb__kb_mechanism_graph` answers with a *set* of edges, ' +
    'which may well be disconnected; `mcp__kb__kb_mechanism_path` answers with the longest *connected* chain ' +
    'through them, ordered and holding at least one verified step, plus the unverified edges adjacent to it. ' +
    'Use the chain whenever you have to state a mechanism, because a set of causal claims that do not join is ' +
    'a list of causes and not a pathway. Use them to resolve an ID you meet, to check ' +
    'whether a claim you are about to make is already recorded, and to build on earlier runs rather than repeat ' +
    'them. They cost no web budget. A record they return may be cited by ID exactly like one quoted below; a ' +
    'record you did not see in either place does not exist, so never invent an ID. ' + loadOnce()
}

// Tools are deferred in this environment: an agent loads a schema with `ToolSearch` before the first call. An agent
// can loop on `ToolSearch` without ever calling the tool; searching is not calling, and the prompt says so.
export function loadOnce() {
  return 'These tools may need loading before their first use: run `ToolSearch` once for all of them together, ' +
    'then CALL them. A tool already returned by `ToolSearch` is ready; searching for it again is a wasted turn ' +
    'and tells you nothing new.'
}

// The datasets of the run. Two read-only tools serve the manifest the `data` phase wrote, and they serve metadata only — reading a data
// file is the job of code that writes a verifiable artifact. `verify` does not mention them,
// as it does not mention the KB tools: its verdict rests on the quote it is handed.
// The Paper2Agent extraction of the source paper, served read-only by `tools.paper.mcp_server`.
// Given to `literature` and `generate` only: a phase that judges claims must not be able
// to re-read the paper, or the verifier is doing the author's job.
export function paperBlock() {
  return 'The source paper itself is readable, through two read-only tools: `mcp__paper__paper_navigate` (the ' +
    'table of contents: every heading with its line range and whether it lies outside the main text) and ' +
    '`mcp__paper__paper_passage` (a passage by heading or line range, markup removed, at most 60 lines — a ' +
    'wider request is refused, not shortened, so ask for a subsection). Navigate first, then read only what you ' +
    'need. Two rules decide whether what you read can become evidence. First, reading is not verifying: a ' +
    'quotation you take from a passage still has to pass `mcp__literature__check_quote`, and only `exact` makes ' +
    'it evidence. Second, when an answer has `outside_main_text: true` the passage is a caption, a table, a ' +
    'reference or front matter — the text is real, but `check_quote` cannot return `exact` for it, so ' +
    'paraphrase it, or cite the dataset reference from the data manifest, instead of quoting it. ' +
    'Each answer carries the file\'s `sha256` and the exact line range; they cost no web budget. ' + loadOnce()
}

// `fetch_paper`, given to `literature` and `review` only and only when the config grants a budget. Measured
// Over 14 archives: 192 of the agents' 760 `check_quote` calls were refused `paper_not_in_store`
// **on a resolvable DOI, PMID or PMCID** — 151 distinct papers an agent had correctly identified and could not
// read. 85 of them never reached a claim at all, 29 arrived in the store later in the same run, and 16 were
// genuinely paywalled — that last part this tool cannot close, and says so in its own answer. The agent
// locates, the code decides: it passes one identifier and nothing else.
export function fetchBlock(budget) {
  if (!budget) return ''
  return `When \`check_quote\` answers \`paper_not_in_store\` and you hold that paper's DOI, PMID or PMCID, you can ` +
    'bring it in yourself: call `mcp__literature__fetch_paper` **once** with that identifier, then call ' +
    '`check_quote` again. The identifier is the only thing it takes — a title, a URL or a sentence is refused, ' +
    `because the paper is fetched from Europe PMC and OpenAlex by its identifier. At most ${budget} papers in ` +
    'this phase, shared with the other agents working alongside you, and a paper already in the store costs ' +
    'nothing, so asking twice for the same one buys nothing. `paywalled` or `unresolvable` means the paper ' +
    'cannot be read here at all: say so in the claim, with the identifier, rather than dropping the finding or ' +
    'quoting from memory. **`failed` is different** — it is a transient error, such as the service refusing a ' +
    'burst of requests, and it is not evidence about the paper: you may call the tool once more, and if it ' +
    'fails again say that the fetch failed, not that the paper is unavailable. ' + loadOnce()
}

export function dataBlock() {
  return 'The run also has local datasets, declared and hashed before any agent ran. Two read-only tools describe ' +
    'them: `mcp__data__data_list` (every dataset: id, kind, size, licence note) and `mcp__data__data_describe` ' +
    '(one dataset by id, with the analyses configured to read it). They return metadata only, never the contents ' +
    'of a file, so use them to know what data exists and what was analysed, and never to state what a dataset ' +
    'contains: computed facts enter the knowledge base as claims of the `compute` phase, which you may cite.'
}

// The prior causal graph of the run: a selection over verified causal claims, packed by the orchestrator
// (`pipeline/prior_graph.py`). Empty is a real answer — the literature of this run may have produced no verified
// causal claim — and saying so is better than letting an agent assume a scaffold exists.
export function priorBlock(edges) {
  if (!edges || edges.length === 0) {
    return 'Prior causal graph: empty. The literature of this run produced no verified causal claim, so there is ' +
      'no scaffold to attach to; build your mechanism from the records below and say which edges you are adding.'
  }
  return `Prior causal graph (${edges.length} edge(s), cause -> effect with the status computed from verified ` +
    'relations):\n' + JSON.stringify(edges, null, 1)
}

// Evidence this system computed itself. A slice ranked on literature alone can leave every computed claim out, so
// the slice reserves a share for them, and this block says what they are.
export function computedBlock() {
  return 'Some claims were produced by this system\'s own analyses, not read from a paper: they carry ' +
    '`origin: computation` and `verification_method: code`, and their `source_refs` name the dataset version and ' +
    'the computation (`dataset:<id>@<hash>`, `result:<analysis>@<digest>`). `verified` means something precise ' +
    'for them: re-running that analysis on those inputs reproduces the number. Cite them by ID exactly like a ' +
    'quoted claim — they are the only evidence here that does not depend on an agent having read a source ' +
    'correctly. A negative result among them is evidence too, not a gap.'
}

// The web budget of each agent, stated in its prompt. A session-level hook enforces it as well; when a call is
// refused the agent must finish with what it has.
export function webBlock(web) {
  if (!web || (!web.searches && !web.fetches)) return 'You have no web access in this phase: work only from what you are given.'
  return `Web budget: at most ${web.searches} web searches and ${web.fetches} page fetches in total. Plan them; ` +
    'when the budget is spent, or a web call is refused, stop searching and answer with what you have, ' +
    'stating what you could not check.'
}

// One agent per hypothesis item; outputs are tagged with the hypothesis they are about.
export async function perHypothesis(a, prefix, prompt) {
  const ids = a.items.map(item => `${prefix}@${item.hypothesis_id}`)
  const results = await parallel(a.items.map((item, i) => () => agent(prompt(item), agentOptions(a, ids[i]))))
  const outputs = []
  const dropped = []
  results.forEach((output, i) => {
    if (output) outputs.push({ agent_id: ids[i], hypothesis_id: a.items[i].hypothesis_id, output })
    else dropped.push(ids[i])
  })
  if (dropped.length) log(`${a.phase}: ${dropped.length} agent(s) returned nothing: ${dropped.join(', ')}`)
  return { phase: a.phase, outputs, dropped }
}

// A role's instructions (agents/<phase>.md) with its `{{name}}` slots filled. One pass over the template, so a
// value is never scanned again. Strict both ways: a slot with no value, or a value with no slot, throws, so a
// renamed slot cannot leave an agent a prompt with a hole in it. A value is rendered as a template literal renders
// it (`String(value)`, so `undefined` reads "undefined"): the text an agent receives is the one it always received.
export function fill(template, values) {
  const used = new Set()
  const out = template.replace(/\{\{([a-z_]+)\}\}/g, (_, key) => {
    if (!Object.hasOwn(values, key)) throw new Error(`fill: no value for {{${key}}}`)
    used.add(key)
    return String(values[key])
  })
  const unused = Object.keys(values).filter(key => !used.has(key))
  if (unused.length) throw new Error(`fill: no slot for ${unused.join(', ')}`)
  return out
}
