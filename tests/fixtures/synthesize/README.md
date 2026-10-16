# `synthesize` delivery cases

Inputs for testing the `execute` -> `synthesize` hand-off alone (`pipeline/synthesize_probe.py`,
`tests/test_synthesize_probe.py`), independent of any archived run and of the shared knowledge base.

- `kb/`: two hypotheses `H-1`, `H-2` of run `FIX-SYNTH`, each with one unverified agent claim, one bought in-silico
  EXP and its `buy` decision. Both EXPs name `ics_cytokine_skew_v1` on the same two inputs, so `select_all`
  computes it once for `EXP-1` and binds `EXP-2` to it as a reuse (ADR 0030). Written by `build_kb.py` through the
  real store (deterministic; not a test; re-run it only if the shape must change). All content is synthetic.
- `cases/reuse_receipt.json`: the case. Its sources point at the synthetic deposit of `tests/fixtures/execute/data/`,
  which is reused, not copied. It is `pl-prod-012`'s shape: a reuse has no receipt of its own, and synthesize
  once looked for one under its own ID.

The probe runs the real `execute` phase (real handler, scripted executor agent) and then the real `synthesize`
phase, with both agents scripted by default; `--live-executor`, `--live-pi` or `--live` (both) make them real and paid,
and `ALS_LIVE_SYNTHESIZE=1` enables the paid test.
