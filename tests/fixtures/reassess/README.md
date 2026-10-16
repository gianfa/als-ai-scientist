# `reassess` delivery cases

Inputs for testing the `reassess` phase alone (`pipeline/reassess_probe.py`, `tests/test_reassess_probe.py`),
independent of any archived run and of the shared knowledge base.

- `kb/`: a minimal knowledge base, in the store's own JSONL format. One hypothesis `H-1` of run `FIX-REASSESS`;
  the claim it is built on, `C-1` (an unverified agent assumption, linked `part_of` `H-1`); two computed claims
  `C-2`, `C-3` of the experiment; the in-silico experiment `EXP-1` testing `C-1`. All content is synthetic and
  says so; it asserts nothing about ALS.
- `experiment-analysis/EXP-1/summary.json`: the artifact the execute report names.
- `cases/*.json`: one delivery each — `run_id`, `kb` and `artifacts` relative to the case file, a `note`, and the
  `execute_report` `reassess` reads.
  - `first_execution`: the analysis ran for the first time, its facts are new rows (`written`).
  - `repeated_execution`: the analysis re-ran and the store recognised every fact, so `written` is empty and
    `seen_again` maps the dropped IDs `C-9`, `C-10` to the kept `C-2`, `C-3`.

To add an edge case, add a case file. If it needs records the knowledge base lacks, add them through
`KnowledgeBase.commit` rather than by hand, so the store validates them, and remove the `.lock` it leaves.
Every case must pass `check_delivery`; `test_every_case_is_a_valid_delivery` enforces it.
