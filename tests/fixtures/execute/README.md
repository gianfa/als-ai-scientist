# `execute` delivery cases

Inputs for testing the `execute` phase alone (`pipeline/execute_probe.py`, `tests/test_execute_probe.py`),
independent of any archived run and of the shared knowledge base.

- `kb/`: a minimal knowledge base, in the store's own JSONL format. One hypothesis `H-1` of run `FIX-EXECUTE`;
  the claim it is built on, `C-1` (an unverified agent assumption, linked `part_of` `H-1`); the in-silico
  experiment `EXP-1` (`analysis_id: ics_cytokine_skew_v1`, `dataset_refs` naming its two sources, tested_claims
  `["C-1"]`); the decision `D-1` buying `EXP-1`. All content is synthetic and says so; it asserts nothing about
  ALS. Unlike `tests/fixtures/reassess/kb/`, this knowledge base holds no computed claim — `execute` itself
  commits the facts, into the probe's own copy, when it runs.
- `data/ics_cytokine_skew_v1/`: a synthetic Michaelis-shaped ICS deposit (4 records, 2 donors per group each, 16
  donors total) and its own source-data spreadsheet, generated once by `build_ics_deposit.py` from the shared
  builders in `tests/ics_fixtures.py` and committed as static files. Re-run that script (it is deterministic,
  not a test, not pytest-collected) only if the fixture's shape must change, and check the output is unchanged
  unless that is the point of the change.
- `cases/*.json`: one delivery each — `run_id`, `kb` and every source `path` relative to the case file, an
  `experiment_id` naming a bought EXP `select_all()` must choose — since ADR 0028 it chooses every eligible
  one, so a case names the EXP it is about rather than the only one that can run — and a `note`.
  - `ics_cytokine_skew_v1`: the case above, `EXP-1`, declaring no `required_metadata`.
  - `ics_cytokine_skew_v1_metadata_required`: `EXP-2`, `D-2`, run `FIX-EXECUTE-2` (a separate run ID: it was
    needed when `select()` took only the first eligible EXP, and is kept so each case runs exactly its own) — `EXP-2` declares `required_metadata` using the exact
    `tools.data.cytometry.METADATA_FIELDS` keys the deposit's `shape` reports, reproducing `pl-prod-009`'s own
    shape (two `dataset_refs`, metadata correctly declared) after the fix that let it resolve.

**Pitfall to avoid in a new case.** `pipeline.experiment_feasibility.assess()` rejects a `dataset_refs` entry
that carries an `@<hash>` suffix not matching the manifest it recomputes from the case's actual files. Write
`Experiment.dataset_refs` as bare `"dataset:<source id>"`, with no `@`, so this never triggers — do not copy a
hash-pinned ref out of a production knowledge base dump.

To add an edge case (for example `hla_panel_compact_v1`): add its own `data/<analysis_id>/` sources, a KB entry
built through `KnowledgeBase.commit` (not by hand — `knowledge_base/validate.py::problems()` checks that
`Experiment.hypothesis_id`/`tested_claims` and `Decision.experiment_ids` resolve to real records), and a case
file naming them. No change to `pipeline/execute_probe.py` is needed: `check_selection` calls the real
`select()`/`assess()`, so any valid delivery is accepted. `test_every_case_is_selectable_as_written` enforces
that every case file is one.
