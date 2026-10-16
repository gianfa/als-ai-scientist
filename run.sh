#!/usr/bin/env bash
#
# ALS AI Scientist — end-to-end discovery run.
#
# This is a thin shim over the phase pipeline, which owns ordering, state, resume,
# provenance, budgets and termination in Python (`pipeline/`). Every argument is
# forwarded unchanged to `python -m pipeline.cli`, so this file holds no logic of its own and cannot
# drift from the system it launches.
#
# A live run is PAID: one `claude -p` session per phase.
#
#   ./run.sh --run-id pl-008                                  # all phases, configs/pipeline.yaml
#   ./run.sh --run-id pl-008 --config configs/pipeline-smoke.yaml   # the cheap variant
#   ./run.sh --run-id pl-008 --phases data,compute            # the two free phases
#   ./run.sh --run-id pl-008 --no-novelty                     # skip the prior-art stage that follows `synthesize`
#   ./run.sh --help                                           # the pipeline CLI's own help
#
# Re-running with the same --run-id resumes after the last completed phase.
# Results: experiments/<run-id>/results/. Documentation: docs/pipeline.md.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

for tool in claude uv; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "run.sh: '$tool' is not on PATH; see docs/quick-start.md" >&2
        exit 2
    fi
done

if [[ $# -eq 0 ]]; then
    echo "run.sh: --run-id is required (a live run is paid). Try: ./run.sh --help" >&2
    exit 2
fi

# `.venv` at the repository root is the convention of the other scripts here, and `${VAR:-default}` was not
# enough to enforce it: this devcontainer *exports* `UV_PROJECT_ENVIRONMENT=/opt/cdqn-venv`, another project's
# environment, so the default never applied and a launch synced this project's dependencies into that venv —
# 24 packages installed and 2 uninstalled there on 2026-10-08, the second time the same trap cost something.
# An inherited value is honoured only when it points inside this checkout; otherwise it is overridden and the
# override is announced, because silently using another project's environment is what went wrong.
case "${UV_PROJECT_ENVIRONMENT:-}" in
    "$SCRIPT_DIR"/*) ;;                                  # an explicit environment inside the checkout: keep it
    /*)
        echo "run.sh: ignoring UV_PROJECT_ENVIRONMENT=$UV_PROJECT_ENVIRONMENT (outside $SCRIPT_DIR);" \
             "using $SCRIPT_DIR/.venv" >&2
        export UV_PROJECT_ENVIRONMENT="$SCRIPT_DIR/.venv"
        ;;
    *) export UV_PROJECT_ENVIRONMENT="$SCRIPT_DIR/.venv" ;;   # unset, empty, or relative: make it explicit
esac

cd "$SCRIPT_DIR"
exec uv run python -m pipeline.cli "$@"
