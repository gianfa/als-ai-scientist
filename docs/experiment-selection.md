# How an experiment gets chosen, bought and run

This traces one hypothesis from "an agent could test this" to a registered analysis actually executing — the
part of the pipeline that decides *which* experiment, among everything proposed, reaches `execute`. Phase
mechanics are in [pipeline.md](pipeline.md); what each registered analysis computes is in
[analyses.md](analyses.md); how to test any of this in isolation, with a live agent and no full run, is in
[testing.md](testing.md).

## The four steps

1. **`design`** proposes experiments for one hypothesis, per `kind`: `in_silico` (a computation over a
   dataset) or `wet_lab` (a proposal for someone else to fund — this system never runs one). An `in_silico`
   proposal names `dataset_refs`, and may name `required_markers`/`required_metadata` and, for the two
   registered analyses, an `analysis_id`.
2. **The host's data-feasibility check** (`pipeline/experiment_feasibility.py::assess`), deterministic, no
   agent: declared requirements against this run's recorded data inventory. The verdict is `data_supported`,
   `missing_inputs`, or `unverified` — never a guess about whether analysis code exists for a `data_supported`
   proposal. `required_metadata`/`required_markers` must name the *exact* keys a source's own inventory
   reports (`tools/data/cytometry.py::METADATA_FIELDS`, `markers`); free prose describing what is needed can
   never match a machine-reported key and is refused as `missing`, not read as a hint.
3. **`arbitrate`** decides `buy` (name the EXP IDs), `rejected` (verified evidence contradicts an
   indispensable claim), or `stop`, for each hypothesis independently. **The feasibility verdict informs the
   arbiter; it does not gate it.** The prompt says so explicitly (`agents/arbitrate.md`): an
   arbiter may buy an experiment that is `missing_inputs` or `unverified`, as long as it names the dependency
   and does not call it runnable now — so a `buy` decision is not proof the data exists, and an `unverified`
   verdict is not proof it was refused. Measured live, twice (`pipeline/design_arbitrate_probe.py`,
   2026-10-09): an arbiter bought an `unverified` proposal both before and after a data-shape fix changed that
   verdict to `data_supported`.
4. **`execute`** runs only a **bought**, `data_supported`, `in_silico` EXP naming a **registered** handler
   (`analysis/experiment_runner.py::HANDLERS`: `hla_panel_compact_v1`, `ics_cytokine_skew_v1`). The agent's one
   tool takes just the EXP ID; it cannot choose the analysis, inputs or parameters. Everything else bought —
   `missing_inputs`/`unverified` in-silico proposals, every `wet_lab` proposal — stays a proposal in the final
   output; nothing here ever reports one as performed.

## What design/arbitrate cannot see, and why

Author identity and the hypothesis's own declared `author_confidence` are stripped from **every** agent-facing
view of a record (`knowledge_base/retrieve.py::BLINDED`, enforced once, in the one function every served tool
and every pushed prompt slice goes through). This is not specific to `design`/`arbitrate`: it applies
uniformly, including to `review`, which states its own blinding explicitly
(`agents/review.md`: "you do not know who wrote it"). It exists because of a measured failure:
in an early run, `kb_get(["H-18"])` answered with `agent_id` and `author_confidence` before this exclusion
existed, and four of six reviewers pulled their own hypothesis's identity straight back out through
`kb_neighbourhood`. **Practically: a hypothesis's declared confidence cannot be used to steer which experiment
`design` proposes or `arbitrate` buys — not even deliberately, by a test fixture — because no agent-facing tool
ever reports it.** What *does* visibly shape the choice is the data a run's manifest actually offers: an
analysis a hypothesis's data cannot support is not a path the designer can honestly call `data_supported`,
whatever it writes in prose.

## Watching this happen without a full run

A full production run costs the whole pipeline to observe one decision. Two isolated, cheaper probes exist:

- `pipeline/execute_probe.py` — a `Decision`/`Experiment` already written; observes only whether `execute`
  then runs the named handler faithfully.
- `pipeline/design_arbitrate_probe.py` — nothing pre-written; observes whether a live `design` agent proposes
  a registered handler and a live `arbitrate` agent buys it, on a fixture whose data manifest offers only one
  handler's inputs. It asserts nothing about which choice is made — no fixture can force one, and `design`
  regularly proposes `wet_lab` experiments the system can only record, not run.

Both are documented with exact commands in [testing.md](testing.md). Neither replaces a full run: one production run
surfaced the data-shape gap `design_arbitrate_probe` independently
reproduced live, and no full run has yet bought `ics_cytokine_skew_v1` unprompted alongside everything else a
real run also proposes.
