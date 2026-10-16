#!/usr/bin/env bash
#
# Static checks used by the git hooks (.pre-commit-config.yaml) and runnable by hand:
# ruff (lint), black (format, check only) and detect-secrets (keys and passwords in the text to commit).
#
#   scripts/dev/code-quality.sh                 # every tracked file
#   scripts/dev/code-quality.sh path/a.py ...   # only these files
#
# It changes nothing. To fix: `.venv/bin/black .` and `.venv/bin/ruff check --fix .`.
# Tools come from the project's .venv (dev group of pyproject.toml), so there is one version source: uv.lock.

set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."

BIN=.venv/bin
for tool in ruff black detect-secrets-hook; do
    [[ -x "$BIN/$tool" ]] || { echo "[fail] $BIN/$tool not found: run scripts/setup/prepare-environment.sh or 'UV_PROJECT_ENVIRONMENT=.venv uv sync --frozen'" >&2; exit 2; }
done

if [[ $# -gt 0 ]]; then
    FILES=("$@")
else
    mapfile -t FILES < <(git ls-files)
fi
mapfile -t PY_FILES < <(printf '%s\n' "${FILES[@]}" | grep -E '\.py$' || true)

failures=0
step() {
    # step <label> <command...>
    local label="$1"; shift
    echo "[info] $label"
    if "$@"; then echo "[ ok ] $label"; else echo "[fail] $label" >&2; failures=$((failures + 1)); fi
}

if [[ ${#PY_FILES[@]} -gt 0 ]]; then
    step "ruff: lint ${#PY_FILES[@]} Python file(s)" "$BIN/ruff" check --no-cache "${PY_FILES[@]}"
    step "black: format check" "$BIN/black" --check --quiet "${PY_FILES[@]}"
else
    echo "[skip] no Python file to lint or format"
fi

if [[ ${#FILES[@]} -gt 0 ]]; then
    step "detect-secrets: keys and passwords in ${#FILES[@]} file(s)" \
        "$BIN/detect-secrets-hook" --baseline .secrets.baseline "${FILES[@]}"
fi

[[ $failures -eq 0 ]] || { echo "[fail] $failures check(s) failed" >&2; exit 1; }
echo "[info] all checks passed"
