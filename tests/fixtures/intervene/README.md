# `intervene` delivery cases

Inputs for testing the `intervene` phase alone together with the host-side drug lookup it feeds
(`pipeline/intervene_drug_lookup_probe.py`, `tests/test_intervene_drug_lookup_probe.py`), independent of any
archived run and of the shared knowledge base.

There is no `kb/` here. `intervene` reads one thing from the store — the hypotheses of the run that the
arbiter did not reject — and `tests/fixtures/execute/kb/` already holds exactly that (`H-1` of run
`FIX-EXECUTE`, with `D-1` buying `EXP-1`, so `H-1` is a survivor). A second synthetic knowledge base would be
a file to keep in step with the first for no gain, so the case points at that one; the probe copies it into
its own directory and never writes to the fixture.

- `cases/*.json`: one delivery each — `run_id`, `kb` relative to the case file, and a `note`.
  - `surviving_hypothesis`: run `FIX-EXECUTE`, whose single surviving hypothesis is what `intervene` is asked
    to propose candidates for.

What the live half of the probe is for, and why no mocked test can replace it: the `drug_name` field is filled
by the agent only when it is sure of a real compound, and the lookup that follows queries MyChem.info over the
network. The mocked tests therefore assert the plumbing (the lookup runs, is off by default, commits what it is
told) and the live test asserts that a real agent names a compound at all and that the real service answers.
