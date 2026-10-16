#!/usr/bin/env bash
#
# One-command setup of a working tree, for the devcontainer (called by .devcontainer/postCreate.sh) and for a
# plain clone. Idempotent: every step checks what is already there and says so. It narrates each step with a
# level tag: [info] what it is doing, [skip] already in place, [todo] something only you can do, [warn], [fail].
#
#   scripts/setup/prepare-environment.sh                          # set up, ask before anything that needs your consent
#   scripts/setup/prepare-environment.sh --dry-run                # say what it would do, change nothing
#   scripts/setup/prepare-environment.sh --skip-data              # no downloads (GWAS, IEDB cache)
#   scripts/setup/prepare-environment.sh --accept-dtu-licence     # the IEDB cache is served under the DTU academic licence (LICENSES.md):
#                                             # only this flag, or a typed "y", accepts it; --yes never does
#
# What it cannot do, and says so at the end: obtain the Michaelis et al. PDF (the publisher serves it to a
# browser only, see docs/data-acquisition.md §1) and run /paper2agent on it (a paid Claude session).

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."

DRY_RUN=0
SKIP_DATA=0
ACCEPT_DTU=0
EXTRACTION="dist/michaelis-2025-agent/skill/michaelis-2025-paper"
GWAS_FILE="data/external/gwas-als/GCST90027164_buildGRCh37.tsv.gz"
TODO=()

info() { printf '[info] %s\n' "$*"; }
skip() { printf '[skip] %s\n' "$*"; }
warn() { printf '[warn] %s\n' "$*" >&2; }
todo() { printf '[todo] %s\n' "$*"; TODO+=("$*"); }
fail() { printf '[fail] %s\n' "$*" >&2; exit 1; }

run() {
    # run <command...>: execute, or only announce it under --dry-run.
    if [[ $DRY_RUN -eq 1 ]]; then printf '[dry-run] %s\n' "$*"; else "$@"; fi
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY_RUN=1; shift ;;
        --skip-data) SKIP_DATA=1; shift ;;
        --accept-dtu-licence) ACCEPT_DTU=1; shift ;;
        -h|--help) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) fail "unknown argument '$1' (see --help)" ;;
    esac
done

[[ $DRY_RUN -eq 1 ]] && info "dry run: nothing below is executed"

# 1. Locked dependencies. UV_PROJECT_ENVIRONMENT is set explicitly: the devcontainer exports another project's.
info "step 1/6: Python dependencies (uv sync --frozen into .venv)"
UV_PROJECT_ENVIRONMENT=.venv run uv sync --frozen

# 2. The Paper2Agent skill, at the commit pinned in the Dockerfile (the image already holds it in /opt).
SKILL_DST="$HOME/.claude/skills/paper2agent"
info "step 2/6: Paper2Agent skill -> $SKILL_DST"
if [[ -d "$SKILL_DST" ]]; then
    skip "already installed"
elif [[ -d /opt/Paper2Agent/skills/paper2agent ]]; then
    info "copying the skill baked into the image (/opt/Paper2Agent)"
    run mkdir -p "$SKILL_DST"
    run cp -R /opt/Paper2Agent/skills/paper2agent/. "$SKILL_DST"/
else
    COMMIT="$(sed -n 's/^ARG PAPER2AGENT_COMMIT=//p' .devcontainer/Dockerfile)"
    [[ -n "$COMMIT" ]] || fail "no PAPER2AGENT_COMMIT in .devcontainer/Dockerfile"
    info "cloning jmiao24/Paper2Agent at the pinned commit ${COMMIT:0:10}"
    TMP="$(mktemp -d)"
    run git clone --quiet https://github.com/jmiao24/Paper2Agent.git "$TMP/p2a"
    run git -C "$TMP/p2a" checkout --quiet --detach "$COMMIT"
    run mkdir -p "$SKILL_DST"
    run cp -R "$TMP/p2a/skills/paper2agent/." "$SKILL_DST"/
    rm -rf "$TMP"
fi

# 3. .claude: in a development checkout it is the same directory as .agents (Claude Code reads one, the project
# documents the other); where there is no .agents it is a plain directory.
info "step 3/6: .claude"
if [[ -e .claude || -L .claude ]]; then
    skip ".claude already exists"
elif [[ -d .agents ]]; then
    run ln -s .agents .claude
else
    run mkdir -p .claude
fi

# 4. The paper extraction: the one input that needs a human. If it is there, expose it as a skill.
info "step 4/6: Michaelis et al. (2025) extraction ($EXTRACTION)"
if [[ -d "$EXTRACTION" ]]; then
    skip "extraction present"
    run mkdir -p .claude/skills
    if [[ -e .claude/skills/michaelis-2025 || -L .claude/skills/michaelis-2025 ]]; then
        skip ".claude/skills/michaelis-2025 already linked"
    else
        info "linking the extraction as the skill michaelis-2025"
        run ln -s "$PWD/$EXTRACTION" .claude/skills/michaelis-2025
    fi
else
    todo "get the article PDF: run 'scripts/data/paper-download.py --format pdf', open the URL it prints, save the file as literature/inbox/michaelis-2025.pdf"
    todo "then, in Claude Code, run the /paper2agent command of docs/data-acquisition.md §1.1 (a paid session)"
    todo "then re-run scripts/setup/prepare-environment.sh: it will link the skill and fetch the IEDB cache"
fi

# 5. GWAS summary statistics (open access, ~188 MB, md5-verified by the download script).
info "step 5/6: ALS GWAS summary statistics ($GWAS_FILE)"
if [[ $SKIP_DATA -eq 1 ]]; then
    skip "--skip-data"
elif [[ -s "$GWAS_FILE" ]]; then
    skip "already downloaded"
else
    info "downloading ~188 MB from the GWAS Catalog (resumable, md5 checked)"
    run scripts/data/gwas-data-download.sh
fi

# 6. IEDB / NetMHCIIpan cache: needs the extraction's tables, and the DTU academic licence.
info "step 6/6: IEDB / NetMHCIIpan prediction cache"
if [[ $SKIP_DATA -eq 1 ]]; then
    skip "--skip-data"
elif [[ ! -d "$EXTRACTION" ]]; then
    skip "needs the extraction of step 4 first"
elif UV_PROJECT_ENVIRONMENT=.venv uv run --frozen python scripts/data/iedb-cache-acquire.py --offline >/dev/null 2>&1; then
    skip "cache already covers the grid"
else
    if [[ $ACCEPT_DTU -eq 0 && -t 0 && $DRY_RUN -eq 0 ]]; then
        warn "the predictor is served under the DTU academic licence: non-profit academic use only, no publishing of its benchmark results (LICENSES.md)"
        read -r -p "[info] accept it and request the predictions now? [y/N] " answer
        case "$answer" in y|Y|yes|YES) ACCEPT_DTU=1 ;; esac
    fi
    if [[ $ACCEPT_DTU -eq 1 || $DRY_RUN -eq 1 ]]; then
        info "requesting the missing predictions (network, may take a while)"
        UV_PROJECT_ENVIRONMENT=.venv run uv run --frozen python scripts/data/iedb-cache-acquire.py
    else
        todo "IEDB cache not fetched: re-run with --accept-dtu-licence once you have read the licence in LICENSES.md"
    fi
fi

# Verdict: the same check the run itself relies on, then what is left for the human.
info "final check: scripts/setup/check-paper2agent.sh"
if [[ $DRY_RUN -eq 1 ]]; then
    printf '[dry-run] scripts/setup/check-paper2agent.sh\n'
else
    scripts/setup/check-paper2agent.sh || warn "the extraction is incomplete (see the todo list below)"
fi

echo
if [[ ${#TODO[@]} -eq 0 ]]; then
    info "setup complete"
else
    info "setup done, ${#TODO[@]} step(s) left for you:"
    for t in "${TODO[@]}"; do printf '  - %s\n' "$t"; done
fi
