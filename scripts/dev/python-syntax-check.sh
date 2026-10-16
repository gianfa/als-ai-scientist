#!/usr/bin/env bash
# Parse Python files with ast.parse (no bytecode written).
# Usage: scripts/dev/python-syntax-check.sh [file.py ...]
# Without arguments, checks every .py file tracked or untracked (not ignored) by git.
set -euo pipefail

if [[ $# -gt 0 ]]; then
    files=("$@")
else
    mapfile -t files < <(git ls-files --cached --others --exclude-standard -- '*.py')
fi

if [[ ${#files[@]} -eq 0 ]]; then
    echo "No Python files to check."
    exit 0
fi

python3 - "${files[@]}" <<'EOF'
import ast
import sys
from pathlib import Path

failed = False
for filename in sys.argv[1:]:
    path = Path(filename)
    if path.suffix != ".py":
        sys.exit(f"Refusing non-Python file: {path}")
    try:
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:
        print(f"FAIL: {path}:{exc.lineno}: {exc.msg}", file=sys.stderr)
        failed = True
    else:
        print(f"OK: {path}")

sys.exit(1 if failed else 0)
EOF
