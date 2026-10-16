#!/usr/bin/env bash
#
# Guided path from a fresh clone to a first smoke-test run. Run it from anywhere inside the repository:
#
#   git clone https://github.com/gianfa/als-ai-scientist
#   scripts/setup/workspace-bootstrap.sh
#
# Order: Claude Code login, article PDF (by hand), GWAS data via scripts/setup/prepare-environment.sh, Paper2Agent
# extraction (a paid Claude session) and its check, the IEDB cache (needs the extraction), pre-commit hooks,
# then an optional smoke test (a paid run, asked first). Re-run it after each manual step: every step checks
# what is already in place and skips it.
#
# Note: --accept-dtu-licence below accepts the DTU academic licence of the IEDB predictor; read LICENSES.md first.

set -euo pipefail

# Every path below is relative to the repository root, whatever directory the script is called from.
cd "$(dirname "${BASH_SOURCE[0]}")/../.."

MICHAELIS_PATH="literature/inbox/michaelis-2025.pdf"
MICHAELIS_P2A_REVIEW_PATH="dist/paper-review/michaelis-2025"
MICHAELIS_EXTRACTION="dist/michaelis-2025-agent/skill/michaelis-2025-paper"
GWAS_PATH="data/external/gwas-als/GCST90027164_buildGRCh37.tsv.gz"

# Colours only on a terminal: in a log or a pipe the escape codes would be noise. Decided once, here: inside a
# $(...) stdout is a pipe, so a test made in the function would never colour a prompt built that way.
if [[ -t 1 ]]; then USE_COLOR=1; else USE_COLOR=0; fi

# Only the message is coloured; the [info]/[warn]/... label keeps the terminal's default colour.
# Blinking blue (SGR 5), for the one question that waits for the user; terminals without blink show plain blue.
color_blue_blink() {
    local text="$1"
    if (( USE_COLOR )); then printf '\033[5;34m%s\033[0m' "$text"; else printf '%s' "$text"; fi
}

color_orange() {
    local text="$1"
    if (( USE_COLOR )); then printf '\033[38;5;208m%s\033[0m' "$text"; else printf '%s' "$text"; fi
}

# A horizontal rule as wide as the terminal (72 columns if tput is missing).
rule() {
    local cols line
    cols="$(tput cols 2>/dev/null || echo 72)"
    printf -v line '%*s' "$cols" ''
    echo "${line// /─}"
}

# 1. Claude Code: the command, and a login (plan or API key). Paper2Agent and the smoke test both need it.
if ! command -v claude >/dev/null 2>&1; then
    echo "[fail] The 'claude' command was not found: use the devcontainer, or install Claude Code first" >&2
    exit 1
fi
# The minimum version is the one the image pins, read from the Dockerfile so the two cannot drift apart.
CLAUDE_MIN_VERSION="$(sed -n 's/^ARG CLAUDE_VERSION=//p' .devcontainer/Dockerfile)"
CLAUDE_FOUND_VERSION="$(claude --version 2>/dev/null | awk '{print $1}')"
if [[ -z "$CLAUDE_MIN_VERSION" || -z "$CLAUDE_FOUND_VERSION" ]]; then
    echo "[fail] Could not read the Claude Code version (found '$CLAUDE_FOUND_VERSION', minimum '$CLAUDE_MIN_VERSION' from .devcontainer/Dockerfile)" >&2
    exit 1
fi
if [[ "$(printf '%s\n%s\n' "$CLAUDE_MIN_VERSION" "$CLAUDE_FOUND_VERSION" | sort -V | head -n 1)" != "$CLAUDE_MIN_VERSION" ]]; then
    echo "[fail] Claude Code $CLAUDE_FOUND_VERSION is older than $CLAUDE_MIN_VERSION, the version the image pins: update it ('claude update')" >&2
    exit 1
fi
echo "[info] Claude Code $CLAUDE_FOUND_VERSION (minimum $CLAUDE_MIN_VERSION)"
if claude auth status 2>/dev/null | grep -q '"loggedIn": *true'; then
    echo "[info] Claude Code is logged in"
elif [[ -n "${ANTHROPIC_API_KEY:-}" ]]; then
    echo "[info] Claude Code will use ANTHROPIC_API_KEY (API billing, not your Claude plan)"
elif [[ -t 0 ]]; then
    echo "[info] Claude Code is not logged in: signing in now (opens a browser page, or prints a link to open)"
    claude auth login
else
    printf '[todo] '; color_orange "Claude Code is not logged in: run 'claude auth login' in a terminal, then run this script again"; echo
    exit 1
fi

# 2. The article PDF: the publisher serves it to a browser only, so the download cannot be scripted.
if [[ ! -f "$MICHAELIS_PATH" ]]; then
    echo "[info] You must download the Michaelis 2025 article by hand, then run this script again"
    scripts/data/paper-download.py --format pdf
    exit 0
else
    echo "[info] Michaelis 2025 article found at $MICHAELIS_PATH"
fi

# 3. Dependencies, the Paper2Agent skill and GWAS data (see scripts/setup/prepare-environment.sh); IEDB comes after step 4.
if [[ ! -f "$GWAS_PATH" ]]; then
    echo "[info] Downloading GWAS and IEDB data"
    scripts/setup/prepare-environment.sh --accept-dtu-licence
else
    echo "[info] GWAS data found at $GWAS_PATH"
fi

# 4. Paper2Agent extraction of the article (a paid Claude session). A non-interactive session declines every
# permission prompt, so the tools the conversion uses are allowed up front: file reads/writes and the skill's
# `uv run .../paper_bundle.py` commands (from its SKILL.md), also in the UV_PROJECT_ENVIRONMENT=.venv form that
# this repository uses and a rule without the prefix does not match. The skill lives outside the repository, so
# its directory is added to the session's scope. Nothing else is granted.
P2A_SKILL_DIR="$HOME/.claude/skills/paper2agent"
P2A_ALLOWED_TOOLS="Read,Write,Edit,Glob,Grep,Skill,Bash(uv run *),Bash(UV_PROJECT_ENVIRONMENT=.venv uv run *),Bash(mkdir *),Bash(ls *)"
if [[ ! -d "$MICHAELIS_EXTRACTION" ]]; then
    echo "[info] Launching Paper2Agent on Michaelis 2025"
    printf '[warn] '; color_orange "Paper2Agent runs with these Claude Code permissions granted automatically, for this session only: $P2A_ALLOWED_TOOLS, with access to $P2A_SKILL_DIR"; echo
    echo "[info] Paper2Agent will now set up $MICHAELIS_P2A_REVIEW_PATH and $MICHAELIS_EXTRACTION. It may take a while: about 5+ minutes."
    claude -p "/paper2agent Convert the paper PDF $MICHAELIS_PATH. Use \
    --name michaelis-2025-paper, \
    --work $MICHAELIS_P2A_REVIEW_PATH, \
    and build the final package with --output $MICHAELIS_EXTRACTION. \
    Keep the default table asset names (table-pPPPP-NNN); do not rename them." \
        --allowedTools "$P2A_ALLOWED_TOOLS" --add-dir "$P2A_SKILL_DIR"
else
    echo "[info] Michaelis 2025 Paper2Agent extraction found at $MICHAELIS_EXTRACTION"
fi
# The session above can end without an extraction (a declined permission, an error) and still exit 0, so the
# result is checked against the paths the smoke run reads; without it the run would stop in its 'data' phase.
if ! scripts/setup/check-paper2agent.sh configs/pipeline-smoke.yaml; then
    echo "[fail] The Paper2Agent extraction is missing or incomplete (see the check above): fix it, then run this script again" >&2
    exit 1
fi

# 4b. IEDB cache: it needs the extraction's tables, so step 3 could not fetch it on a first pass.
if UV_PROJECT_ENVIRONMENT=.venv uv run --frozen python scripts/data/iedb-cache-acquire.py --offline >/dev/null 2>&1; then
    echo "[info] IEDB cache already covers the grid"
else
    echo "[info] Fetching the IEDB cache and linking the extraction as a skill"
    scripts/setup/prepare-environment.sh --accept-dtu-licence
fi

# 5. Git hooks, once the repository carries their configuration.
if [[ -f .pre-commit-config.yaml ]]; then
    echo "[info] Installing pre-commit hooks"
    .venv/bin/pre-commit install --hook-type pre-commit --hook-type pre-merge-commit
else
    printf '[skip] '; color_orange "'.pre-commit-config.yaml' not found, skipping"; echo
fi

# 6. Smoke test of the pipeline (a paid run, so it asks first).
SMOKE_CONFIG="configs/pipeline-smoke.yaml"
rule
echo "[info] Last step: a smoke test runs the whole pipeline once, in its smallest configuration"
echo "[info] Its configuration file is $SMOKE_CONFIG: see in particular its 'objective', the question the run works on"
echo "[info] It may take up to about 20 minutes"
printf '[warn] '; color_orange "It calls Claude, so how it is charged"
echo " depends on your Claude plan (usage allowance or API billing)"
echo "[warn] Typical spend is around 8 USD-equivalent; the cap is indicative: set run_max_cost_usd in $SMOKE_CONFIG"
rule
read -r -p "[info] $(color_blue_blink "Shall we run the smoke test? [y/N]") " answer || answer=""
case "$answer" in
    y|Y|yes|YES)
        SMOKE_RUN_ID="beg-001"
        echo "[info] Launching the smoke test: it creates the experiment experiments/$SMOKE_RUN_ID/ (results in experiments/$SMOKE_RUN_ID/results/)"
        ./run.sh --run-id "$SMOKE_RUN_ID" --config "$SMOKE_CONFIG" ;;
    *) echo "[skip] smoke test not run" ;;
esac
