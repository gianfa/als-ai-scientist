"""Append-only, file-based run store.

Layout: `<root>/<run_id>/context.json` (written once) and `<root>/<run_id>/events.jsonl`.
Nothing here overwrites or deletes existing data.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from .events import RUN_ID_PATTERN, Event

CONTEXT_FILE = "context.json"
EVENTS_FILE = "events.jsonl"


class RunNotFoundError(LookupError):
    pass


def git_commit(cwd: Path | None = None) -> str | None:
    """Current commit hash, or None outside a git repository."""
    try:
        result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=cwd, capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout.strip()


class RunStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def run_dir(self, run_id: str) -> Path:
        # Also guards the HTTP server against path traversal.
        if not RUN_ID_PATTERN.match(run_id):
            raise RunNotFoundError(run_id)
        return self.root / run_id

    def create_run(self, run_id: str, context: dict[str, Any]) -> Path:
        path = self.run_dir(run_id)
        path.mkdir(parents=True, exist_ok=False)  # never reuse a run directory
        with (path / CONTEXT_FILE).open("x", encoding="utf-8") as f:
            json.dump({"run_id": run_id, **context}, f, indent=2)
        (path / EVENTS_FILE).touch(exist_ok=False)
        return path

    def append(self, event: Event) -> None:
        path = self.run_dir(event.run_id) / EVENTS_FILE
        if not path.exists():
            raise RunNotFoundError(event.run_id)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")

    def read_context(self, run_id: str) -> dict[str, Any]:
        path = self.run_dir(run_id) / CONTEXT_FILE
        if not path.exists():
            raise RunNotFoundError(run_id)
        return json.loads(path.read_text(encoding="utf-8"))

    def read_events(self, run_id: str) -> list[Event]:
        path = self.run_dir(run_id) / EVENTS_FILE
        if not path.exists():
            raise RunNotFoundError(run_id)
        events = []
        with path.open(encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    events.append(Event.from_dict(json.loads(line)))  # malformed lines fail loudly
        return events

    def list_runs(self) -> list[str]:
        if not self.root.exists():
            return []
        return sorted(p.name for p in self.root.iterdir() if p.is_dir() and RUN_ID_PATTERN.match(p.name))
