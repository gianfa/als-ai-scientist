#!/bin/bash
# Launch the wf_observer web UI (per-run views: Overview, Workflow, Phase graph, Agents, Story, Tokens, Evaluation).
#
# Usage: scripts/ops/launch-observer-ui.sh [--host HOST] [--port PORT] [--smoke-root DIR]
#   Defaults: host 127.0.0.1, port 8765 (the CLI's own defaults). Extra flags go to
#   `observability.wf_observer.cli serve` unchanged; run with --help to list them.
#
# Exit codes: 1 if the port is already in use, otherwise the server's own exit code.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PACKAGE_ROOT="$REPO_ROOT"

PORT=8765
args=("$@")
for ((i = 0; i < ${#args[@]}; i++)); do
  [[ "${args[$i]}" == "--port" ]] && PORT="${args[$((i + 1))]:-$PORT}"
  [[ "${args[$i]}" == --port=* ]] && PORT="${args[$i]#--port=}"
done

if command -v ss >/dev/null && ss -ltn "sport = :$PORT" | grep -q LISTEN; then
  echo "error: port $PORT is already in use (is the UI already running? open http://127.0.0.1:$PORT/)" >&2
  exit 1
fi

cd "$PACKAGE_ROOT"
# Force the package's own venv: the container may point UV_PROJECT_ENVIRONMENT at another project.
# RAYON_NUM_THREADS=1 avoids uv's thread-pool panic in this container.
exec env UV_PROJECT_ENVIRONMENT=.venv VIRTUAL_ENV= RAYON_NUM_THREADS=1 \
  uv run --quiet python -m observability.wf_observer.cli serve "$@"
