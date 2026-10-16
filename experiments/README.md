# `experiments/`

One directory per run, named by its `--run-id`, holding everything that run produced:

```
experiments/<RUN_ID>/results/
  state.json                 phases completed, and each phase's launch record (cost, wall time)
  context.json               claude_version, resolved model ids, git commit, configuration digest
  NN-<phase>-report.json     one per phase, in the order the orchestrator ran them
  final-proposal.json        written by `synthesize`, and only once it has run
  figures/                   diagnostic plots plus the `report.txt` that states what each one shows
experiments/<RUN_ID>-nohup.log   the run's own stdout, kept beside its archive
```

**The contents are never committed.** A run archive holds third-party derived material and is large; it is
the kind of thing a Zenodo deposit is for, not a git repository. `.gitignore` keeps everything here out
except this file, which keeps the directory present in a fresh clone, before any run has been made.

A fresh clone therefore has no archive at all, and that is the expected state. To produce one, see
[First run](../README.md#first-run-new-user); to read one, [Read a result](../README.md#read-a-result) and
[Observer UI](../docs/tools/observer-ui.md).

Nothing is ever overwritten: re-running the same `--run-id` resumes after the last completed phase instead
of starting the archive again.
