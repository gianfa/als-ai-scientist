# `results/`

Working output of the implementation, written outside any single run's archive: the deterministic analyses
write here when they are driven directly (`analysis/cli.py`), and
`results/smoke/` holds the output of the smoke checks.

**The contents are never committed.** What belongs to a run belongs instead to that run's own archive under
[`experiments/<RUN_ID>/results/`](../experiments/README.md), sealed with a digest, which is what the
evaluation and the technical report read. Anything here is reproducible from code plus the declared inputs,
or it is not a result worth citing.

`.gitignore` keeps everything here out except this file, so that the directory is present in a fresh clone,
where it is legitimately empty.
