#!/usr/bin/env bash
#
# Check the Paper2Agent inputs a run needs: the extraction of Michaelis et al. (2025), and the skill that
# produces it.
#
# Why this is not just an "is the skill installed" check. The analyses never read the PDF; they read files
# the extraction writes, addressed by path (the two table sources in configs/pipeline*.yaml, references/paper.md
# in pipeline/launcher.py). An extraction that landed elsewhere, or whose tables were renamed, satisfies "the skill
# is installed" and still stops the `data` phase. So the expected table paths are read out of the configuration itself rather than repeated
# here: a config/extraction mismatch is the failure this script exists to catch.
#
# Usage: scripts/setup/check-paper2agent.sh [config.yaml]        (default: configs/pipeline.yaml)
#
# Exit: 0 if the extraction is complete, 1 otherwise. A missing Paper2Agent skill is reported but does not
# fail the check on its own: the skill is needed to *build* the extraction, not to read one that exists.
#
# PAPER2AGENT_ROOT overrides the directory the configuration's relative paths resolve against. It defaults
# to the repository root and exists so the tests can check this script against a tree they control.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CONFIG="${1:-configs/pipeline.yaml}"
cd "${PAPER2AGENT_ROOT:-$REPO_ROOT}"

failures=0

report() {
    # report <ok|fail|note> <message>
    case "$1" in
        ok) printf '  OK    %s\n' "$2" ;;
        fail) printf '  FAIL  %s\n' "$2"; failures=$((failures + 1)) ;;
        note) printf '  note  %s\n' "$2" ;;
    esac
}

echo "Paper2Agent skill (needed only to build the extraction)"
if [[ -d "$HOME/.claude/skills/paper2agent" ]]; then
    report ok "$HOME/.claude/skills/paper2agent"
else
    report note "not installed at $HOME/.claude/skills/paper2agent — the devcontainer installs it (docs/devcontainer.md)"
fi

echo "Extraction (docs/data-acquisition.md section 1.1)"
if [[ ! -f "$CONFIG" ]]; then
    report fail "$CONFIG: no such configuration, so the expected table paths are unknown"
    echo "check-paper2agent: $failures problem(s)" >&2
    exit 1
fi

# The extraction root is the parent of assets/, i.e. what Paper2Agent was given as --output.
SOURCE_ROOT="$(grep -oE 'dist/[^ "]*michaelis-2025-paper' "$CONFIG" | head -1)"
if [[ -z "$SOURCE_ROOT" ]]; then
    report fail "$CONFIG names no extraction under dist/, so there is nothing to check"
else
    if [[ -d "$SOURCE_ROOT" ]]; then
        report ok "$SOURCE_ROOT"
    else
        report fail "$SOURCE_ROOT is missing — this is Paper2Agent's --output path, see docs/data-acquisition.md section 1.1"
    fi

    # references/paper.md is the Markdown rendering the launcher gives the agents.
    if [[ -f "$SOURCE_ROOT/references/paper.md" ]]; then
        report ok "$SOURCE_ROOT/references/paper.md"
    else
        report fail "$SOURCE_ROOT/references/paper.md is missing (read by pipeline/launcher.py)"
    fi
fi

# The tables, exactly as the configuration addresses them: renaming them breaks the run.
mapfile -t TABLES < <(grep -oE 'dist/[^ "]*/assets/table/[^ "]+\.csv' "$CONFIG" | sort -u)
if [[ ${#TABLES[@]} -eq 0 ]]; then
    report fail "$CONFIG declares no table under assets/table/ — expected the two Michaelis tables"
fi
for table in "${TABLES[@]}"; do
    if [[ -s "$table" ]]; then
        report ok "$table"
    elif [[ -f "$table" ]]; then
        report fail "$table is empty"
    else
        report fail "$table is missing — keep Paper2Agent's default asset names (table-pPPPP-NNN)"
    fi
done

if [[ $failures -eq 0 ]]; then
    echo "check-paper2agent: the extraction $CONFIG expects is present"
    exit 0
fi
echo "check-paper2agent: $failures problem(s); the data phase would stop on them" >&2
exit 1
