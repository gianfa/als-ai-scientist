"""The single answer to "which runs exist, and where does each one live".

If every view discovered runs on its own, with its own roots and id patterns, a run id offered by
one view could 404 in another — a run named `my-run-002`, as `docs/quick-start.md` documents, would
be listed by `/runs` and rejected by `/tokens`. One catalog serves every endpoint instead.

Two run sources are peers:

- `experiments/<id>/results/` — written by `run.sh`, the normal case;
- the run store — written through the observability API. A store run names its experiment in
  `context.json` (`name`), so the same run is not listed twice.

Smoke runs are a third, lesser kind, left by a smoke script of the previous architecture without `run.sh`'s
contract (no `context.json`, no stages, often no result), so they are excluded unless asked for.

Path roots are explicit constructor parameters with defaults, so tests inject temporary directories
and every endpoint resolves against the same roots.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

from ._config import EXPERIMENTS_ROOT, SMOKE_ROOT
from .store import RunNotFoundError, RunStore

RunSource = Literal["experiment", "store", "smoke"]

# Run ids reach the filesystem as a path segment, so they must stay inert. Deliberately broad: any
# narrower pattern rejects valid run ids.
SAFE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")

# What a state probe returns: (status, finished). Injected so this module stays independent of the
# server's stage logic, which is the single source of truth for it.
StateFn = Callable[[Path], "tuple[str, bool]"]


def is_safe_name(name: str) -> bool:
    return bool(name) and name != "." and name != ".." and bool(SAFE_NAME_PATTERN.match(name))


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _read_json(path: Path) -> dict[str, Any] | None:
    """Parsed JSON object, or None when missing or unreadable: a listing must never fail on one run."""
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


@dataclass(frozen=True)
class RunRef:
    """One run, resolved: where its artifacts are and what state it is in."""

    id: str
    display: str
    source: RunSource
    results_dir: Path
    status: str = "unknown"
    finished: bool = False
    started_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "display": self.display,
            "source": self.source,
            "status": self.status,
            "finished": self.finished,
        }


class RunCatalog:
    """Lists and resolves runs. Construct once per server and share it."""

    def __init__(
        self,
        experiments_root: Path = EXPERIMENTS_ROOT,
        store: RunStore | None = None,
        smoke_root: Path = SMOKE_ROOT,
        state_fn: StateFn | None = None,
    ) -> None:
        self.experiments_root = Path(experiments_root)
        self.smoke_root = Path(smoke_root)
        self.store = store
        # Without a probe every run reads as unknown/unfinished rather than failing: the catalog's
        # job is to resolve runs, not to decide their outcome.
        self._state_fn: StateFn = state_fn or (lambda _: ("unknown", False))

    # -- resolution ---------------------------------------------------------

    def experiment_results_dir(self, run_id: str) -> Path:
        return self.experiments_root / run_id / "results"

    def _state(self, results_dir: Path) -> tuple[str, bool]:
        try:
            return self._state_fn(results_dir)
        except (OSError, ValueError):
            return "unknown", False

    def _experiment_ref(self, run_id: str) -> RunRef | None:
        results_dir = self.experiment_results_dir(run_id)
        if not results_dir.is_dir():
            return None
        status, finished = self._state(results_dir)
        # Ordering prefers the artifacts written latest, but any populated `results/` is a run: a
        # stage that died before writing context.json still leaves `run_exit_code`, and dropping
        # such a run from the selector would hide exactly the runs worth inspecting.
        started = _mtime(results_dir / "evaluation" / "evaluation.json") or _mtime(results_dir / "context.json")
        if not started and any(results_dir.iterdir()):
            # Fallback only: the artifacts above order the listing when present, and using the
            # directory's own mtime alongside them would flatten those differences to "now".
            started = _mtime(results_dir)
        return RunRef(run_id, run_id, "experiment", results_dir, status, finished, started)

    def _smoke_ref(self, run_id: str) -> RunRef | None:
        results_dir = self.smoke_root / run_id
        if not results_dir.is_dir():
            return None
        status, finished = self._state(results_dir)
        return RunRef(run_id, run_id, "smoke", results_dir, status, finished, _mtime(results_dir))

    def _store_context(self, run_id: str) -> dict[str, Any] | None:
        if self.store is None:
            return None
        try:
            return self.store.read_context(run_id)
        except (RunNotFoundError, OSError, json.JSONDecodeError, ValueError):
            return None

    def _store_ref(self, run_id: str) -> RunRef | None:
        if self.store is None:
            return None
        try:
            run_dir = self.store.run_dir(run_id)
        except RunNotFoundError:
            return None
        if not run_dir.is_dir():
            return None

        display = run_id
        results_dir = run_dir
        context = self._store_context(run_id)
        if context:
            # Store runs carry `name`; runs recorded by the launcher carry `run_id`.
            experiment_id = context.get("name") or context.get("run_id") or run_id
            workflow_id = context.get("workflow_run_id")
            display = f"{experiment_id} | {workflow_id}" if workflow_id else str(experiment_id)
            # The artifacts a store run describes live in its experiment directory, when it has one.
            if experiment_id and is_safe_name(str(experiment_id)):
                candidate = self.experiment_results_dir(str(experiment_id))
                if candidate.is_dir():
                    results_dir = candidate

        status, finished = self._state(results_dir)
        return RunRef(run_id, display, "store", results_dir, status, finished, _mtime(run_dir))

    def get(self, run_id: str, *, include_smoke: bool = True) -> RunRef | None:
        """Resolve one run id, or None if it is unknown or unsafe as a path segment."""
        if not is_safe_name(run_id):
            return None
        for ref in (self._store_ref(run_id), self._experiment_ref(run_id)):
            if ref is not None:
                return ref
        return self._smoke_ref(run_id) if include_smoke else None

    # -- listing ------------------------------------------------------------

    def list(self, *, include_smoke: bool = False) -> list[RunRef]:
        """Runs for the UI selector, newest first.

        Store runs first, so an experiment already named by one is not listed twice. Smoke runs are
        excluded by default: they lack the artifacts the run views expect.
        """
        refs: list[RunRef] = []
        named_experiments: set[str] = set()

        for run_id in self.store.list_runs() if self.store else []:
            ref = self._store_ref(run_id)
            if ref is None:
                continue
            context = self._store_context(run_id)
            if context:
                experiment_id = context.get("name") or context.get("run_id")
                if experiment_id:
                    named_experiments.add(str(experiment_id))
            refs.append(ref)

        if self.experiments_root.is_dir():
            for experiment in self.experiments_root.iterdir():
                if not experiment.is_dir() or experiment.name in named_experiments:
                    continue
                if not is_safe_name(experiment.name):
                    continue
                ref = self._experiment_ref(experiment.name)
                # An empty `results/` (or none at all) is scaffolding, not a run.
                if ref is not None and ref.started_at > 0:
                    refs.append(ref)

        if include_smoke and self.smoke_root.is_dir():
            # Also against the experiment names store runs stand in for: a store run listed under
            # its own id still represents `named_experiments`, so a smoke dir of that name is a
            # duplicate too.
            listed = {ref.id for ref in refs} | named_experiments
            for smoke in self.smoke_root.iterdir():
                if not smoke.is_dir() or smoke.name in listed or not is_safe_name(smoke.name):
                    continue
                ref = self._smoke_ref(smoke.name)
                if ref is not None:
                    refs.append(ref)

        # Newest first. Ties are real: directories written in the same burst can share an mtime to
        # the filesystem's precision, and run ids are timestamps often enough that the higher id is
        # the later run. Sorted in two stable passes so the id only breaks ties.
        refs.sort(key=lambda ref: ref.id, reverse=True)
        refs.sort(key=lambda ref: ref.started_at, reverse=True)
        return refs
