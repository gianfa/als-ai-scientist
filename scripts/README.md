# Helper Scripts

Helpers around the pipeline. The pipeline never calls them; some prepare what it needs.

**From a fresh clone to a first run:**

```bash
scripts/setup/workspace-bootstrap.sh
```

It walks you through every step and can be re-run after each manual one. Details: `docs/quick-start.md`.

| Folder | What for |
|---|---|
| `setup/` | Prepare a working tree |
| `data/` | Download the inputs a run reads (`docs/data-acquisition.md`) |
| `ops/` | Inspect runs; `ops/launch-observer-ui.sh` opens the web UI |
| `dev/` | Code and reproducibility checks, for contributors |
