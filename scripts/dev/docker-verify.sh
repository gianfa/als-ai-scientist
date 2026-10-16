#!/usr/bin/env bash
# Build the devcontainer image and run the deterministic test suites inside it.
# Usage: scripts/dev/docker-verify.sh [image-tag]
# Does NOT run ./run.sh (it needs credentials and spends API budget). The default suites are mocked.
# Side effects on the checkout: none; the Python venv lives in the container's /tmp and is discarded.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
IMAGE="${1:-als-ai-scientist-dev}"
WORKDIR=/app/als-ai-scientist

command -v docker >/dev/null || { echo "error: docker not found" >&2; exit 2; }

docker build \
    -f "$ROOT/.devcontainer/Dockerfile" \
    --build-arg UID="$(id -u)" --build-arg GID="$(id -g)" \
    -t "$IMAGE" "$ROOT"

docker run --rm \
    --cap-drop ALL --security-opt no-new-privileges:true \
    --user "$(id -u):$(id -g)" \
    --mount "type=bind,source=$ROOT,target=$WORKDIR" \
    -w "$WORKDIR" -e UV_PROJECT_ENVIRONMENT=/tmp/venv -e HOME=/tmp/home \
    "$IMAGE" bash -euo pipefail -c '
        scripts/dev/python-syntax-check.sh
        uv sync --frozen
        uv run pytest
        node --test tests/workflow/*.test.js
    '
