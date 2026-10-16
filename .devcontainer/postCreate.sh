#!/usr/bin/env bash
# Runs once after the devcontainer is created. The setup itself lives in scripts/setup/prepare-environment.sh, shared with the
# plain-clone route. Data downloads are left out here (they are large and some need a licence to be accepted):
# run `scripts/setup/prepare-environment.sh` from a terminal to fetch them.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
scripts/setup/prepare-environment.sh --skip-data
echo "Ready. Next: run scripts/setup/workspace-bootstrap.sh (Claude login, data, first run)."
