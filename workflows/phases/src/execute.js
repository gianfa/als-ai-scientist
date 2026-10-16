import { fill, agentOptions, collect, requirePhaseArgs } from './lib/phase.js'
import ROLE from '../../../agents/execute.md'

export const meta = {
  name: 'phase-execute',
  description: 'One computational agent per bought experiment calls a bounded registered tool and interprets its verified summary.',
  phases: [{ title: 'execute' }],
}

const INJECTED_ARGS = null // @@INJECTED_ARGS@@

const A = requirePhaseArgs(INJECTED_ARGS ?? args, 'execute', ['selected'])
if (!Array.isArray(A.selected) || A.selected.length === 0) throw new Error('execute: selected must be a non-empty array of EXPs')
for (const s of A.selected) {
  if (!s || !s.experiment_id) throw new Error('execute: every selected entry needs an experiment_id')
}
phase('execute')

// What each registered analysis tests, and the one interpretation each outcome licenses
// (`analysis/experiment_runner.py`, HANDLERS[...].interpretations).
const ANALYSES = {
  hla_panel_compact_v1: `The registered analysis tests whether at most eight recognized peptides cover the four HLA-II
alleles in the local prediction grid at percentile rank <=10. The control is all size-matched subsets of the same
recognized peptides. This is a prediction-panel diagnostic only. It cannot establish population HLA coverage, antigen
presentation, T-cell response, clinical benefit or a therapeutic intervention. If the outcome is
\`panel_condition_met\`, set interpretation to \`supports_panel_condition_only\`; if it is
\`panel_condition_failed\`, use \`weakens_panel_condition_only\`.`,
  ics_cytokine_skew_v1: `The registered analysis re-analyses the Michaelis intracellular cytokine staining deposit: per
donor, the CD4+ cytokine response to the C9orf72 peptide pool minus the vehicle well, from the counts FlowJo saved
for the authors' gates, then compares the ratio IFNg / (IL-4 + IL-10) between ALS and healthy controls with an
exact two-sided Mann-Whitney test, and reports how many donors reproduce the published source data. It concerns
cultures expanded in vitro for two weeks; it cannot establish ex vivo frequencies, causation, HLA or genotype
effects, or clinical benefit. If the outcome is \`skew_replicated\`, set interpretation to
\`supports_ifng_skew_only\`; if it is \`skew_not_replicated\`, use \`weakens_ifng_skew_only\`.`,
}

// One agent per bought EXP. They share this session's `mcp__experiment__run_experiment` tool, which authorizes
// the whole selected set, because the server is registered per stage and not per agent. Each agent
// is therefore told which single EXP is its own, and the host binds each artifact to its EXP afterwards.
const prompt = selected => fill(ROLE, {
  experiment_id: selected.experiment_id,
  analysis_rule: ANALYSES[selected.analysis_id] ?? 'The selected analysis is not registered: return no result.',
  experiment: JSON.stringify(selected, null, 1),
})

const ids = A.selected.map(selected => `executor@${selected.experiment_id}`)
const results = await parallel(A.selected.map((selected, i) => () => agent(prompt(selected), agentOptions(A, ids[i]))))
return collect('execute', ids, results)
