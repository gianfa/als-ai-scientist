# Devcontainer

`.devcontainer/` defines the reproducible development and execution environment.

## What it contains

| Component | Version | Why |
|---|---|---|
| Python | 3.10 (base image `python:3.10-slim-bookworm`) | `pyproject.toml` |
| uv | 0.12.19 | Python dependency management (`uv.lock`, `--frozen`) |
| Node.js | 22.23.3 | builds and tests the phase workflows (no npm dependency) |
| Claude Code | 2.1.284 | `run.sh` launches the pipeline phases through the `claude` CLI |
| Paper2Agent skill | commit `8c2d059` | paper-to-agent conversion |
| LaTeX (TeX Live, bibtex) + pandoc | Debian bookworm packages | report PDF export |
| jq, git, curl | Debian bookworm packages | `scripts/ops/check-running-workflows.sh` |

Versions are build args in `.devcontainer/Dockerfile`. The Debian base image and apt packages are
not pinned by digest, so the image is reproducible in its toolchain versions, not bit-for-bit.

## What it does not contain

- Credentials. Set `ANTHROPIC_API_KEY` on the host (read via `${localEnv:...}`), or run `claude`
  once inside the container to log in; the login is kept in the named volume
  `als-ai-scientist-claude-home`.
- The Michaelis et al. (2025) Paper2Agent extraction
  (`dist/michaelis-2025-agent/skill/michaelis-2025-paper`): CC BY-NC-ND, local only. See
  [Quick start](quick-start.md).
- Any guarantee on run outputs. The image pins the tools; runs still depend on a remote,
  non-deterministic model. Reproducibility of results rests on the run provenance and checkpoints.

## VS Code

1. Install the *Dev Containers* extension and Docker.
2. Open the repository and choose **Reopen in Container**.
3. `postCreate.sh` runs `scripts/setup/prepare-environment.sh --skip-data`: `uv sync --frozen` in the
   repository root (it fails if the lock file is out of sync), the Paper2Agent skill into `~/.claude/skills`
   and the `.claude` link. The data downloads are left out; run `scripts/setup/workspace-bootstrap.sh` for them.
4. The observer UI port 8765 is forwarded.

## Without VS Code

```bash
docker build -f .devcontainer/Dockerfile \
  --build-arg UID="$(id -u)" --build-arg GID="$(id -g)" -t als-ai-scientist-dev .
docker run -it --rm --cap-drop ALL --security-opt no-new-privileges:true \
  --user "$(id -u):$(id -g)" -e ANTHROPIC_API_KEY -e HOME=/home/vscode \
  -p 127.0.0.1:8765:8765 \
  --mount type=bind,source="$PWD",target=/app/als-ai-scientist \
  -w /app/als-ai-scientist als-ai-scientist-dev bash
# inside: bash .devcontainer/postCreate.sh
```

## Verification

`scripts/dev/docker-verify.sh` builds the image and runs the syntax check, the pytest, TypeScript and
workflow suites inside it. It does not run `./run.sh`.
The observer UI binds to `127.0.0.1` by default; to reach it through a published port from inside
Docker start it with `UI_HOST=0.0.0.0 scripts/ops/launch-observer-ui.sh` (only with the port published to
`127.0.0.1` as above).
