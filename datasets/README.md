# `datasets/`

Datasets **derived by this project** from the inputs in `data/`: anything we compute, filter, join or
re-encode and then want to cite as a dataset in its own right.

It is **empty**, and that is a true statement about the project rather than a placeholder: every analysis so
far reads `data/external/` directly and writes its output into the run archive under
`experiments/<RUN_ID>/results/`, sealed by a digest. No intermediate dataset has been promoted to a citable
artifact.

The distinction this directory exists to keep:

- `data/` — third-party inputs, acquired by the user, git-ignored, never redistributed
  (`docs/data-acquisition.md`, `LICENSES.md`).
- `datasets/` — our own derived data, which we may redistribute, subject to the licence of what it derives
  from.

A file placed here must arrive with its provenance: what it derives from, by which code, and under which
licence the derivation may be shared.
