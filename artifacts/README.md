# `artifacts/`

Generated files that belong to no single run, such as an export saved by the observer UI.

**The contents are never committed.** They are rebuilt from the code and the configuration, so a stale file here
describes a previous version of the code, not a result.

`.gitignore` keeps everything here out except this file, so that the directory is present in a fresh clone and
`scripts/dev/verify_project_requirements.py` finds it.
