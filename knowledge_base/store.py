"""Append-only JSONL storage of the knowledge base, with a single writer.

One file per record type under the KB root. Records are never rewritten or deleted: a correction is a new record
(`supersedes`), a re-encountered claim is a `seen_again` event.

- `read_records(root)` reads the KB without a lock, for readers (retrieval, the evaluator). A last line without its
  newline is a write in progress and is skipped; any other malformed line raises, because a KB that cannot be read
  exactly cannot be trusted.
- `KnowledgeBase.open(root)` is the only way to write. It takes an exclusive, non-blocking lock on `root/.lock` for
  its whole lifetime, so a second writer fails at once instead of interleaving.

`commit(records, run_id)` writes a batch in order. A claim whose `canonical_hash` is already in the KB is not
written: an event `seen_again` is appended and later records of the batch that point at the proposed ID are
redirected to the existing one. A record that fails validation is not written: an event `rejected` with the
reasons is appended, and records depending on it fail in turn as "unknown". Nothing in a batch raises on a bad
proposal; I/O errors do raise.
"""

from __future__ import annotations

import fcntl
import os
import re
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Final

from pydantic import BaseModel

from .schemas import (
    PREFIXES,
    Claim,
    Decision,
    Entity,
    Event,
    Experiment,
    Hypothesis,
    Ignorance,
    Intervention,
    Relation,
)
from .validate import Record, problems

FILES: Final[dict[type[BaseModel], str]] = {
    Claim: "claims.jsonl",
    Relation: "relations.jsonl",
    Entity: "entities.jsonl",
    Hypothesis: "hypotheses.jsonl",
    Ignorance: "ignorance.jsonl",
    Event: "events.jsonl",
    Experiment: "experiments.jsonl",
    Decision: "decisions.jsonl",
    Intervention: "interventions.jsonl",
}
KINDS: Final[dict[str, type[BaseModel]]] = {
    "claim": Claim,
    "relation": Relation,
    "entity": Entity,
    "hypothesis": Hypothesis,
    "ignorance": Ignorance,
    "event": Event,
    "experiment": Experiment,
    "decision": Decision,
    "intervention": Intervention,
}
LOCK_FILE: Final = ".lock"


class KnowledgeBaseError(RuntimeError):
    """The KB on disk is unreadable, or a second writer tried to open it."""


def read_records(root: Path) -> dict[str, Record]:
    """Every record of the KB, by ID, in file order. Missing files mean no records of that type."""
    records: dict[str, Record] = {}
    for model, name in FILES.items():
        path = root / name
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        lines = text.split("\n")
        complete = lines[:-1]  # the part after the last newline is empty, or a write in progress
        for number, line in enumerate(complete, 1):
            if not line.strip():
                continue
            try:
                record = model.model_validate_json(line)
            except ValueError as err:
                raise KnowledgeBaseError(f"{path}:{number}: unreadable record: {err}") from err
            if record.id in records:
                raise KnowledgeBaseError(f"{path}:{number}: duplicate id {record.id}")
            records[record.id] = record
    return records


@dataclass
class CommitResult:
    written: list[str] = field(default_factory=list)
    seen_again: dict[str, str] = field(default_factory=dict)  # proposed claim ID -> existing claim ID
    rejected: dict[str, list[str]] = field(default_factory=dict)  # proposed ID -> reasons


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class KnowledgeBase:
    """The single writer. Use only through `KnowledgeBase.open(root)`."""

    def __init__(self, root: Path, records: dict[str, Record], clock: Callable[[], datetime]) -> None:
        self.root = root
        self._records = records
        self._clock = clock
        self._by_hash = {r.canonical_hash: r.id for r in records.values() if isinstance(r, Claim)}
        self._next = {
            prefix: 1 + max((int(i.split("-")[1]) for i in records if i.startswith(prefix + "-")), default=0)
            for prefix in PREFIXES.values()
        }

    @classmethod
    @contextmanager
    def open(cls, root: Path, clock: Callable[[], datetime] = _utc_now) -> Iterator[KnowledgeBase]:
        root.mkdir(parents=True, exist_ok=True)
        with open(root / LOCK_FILE, "a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as err:
                raise KnowledgeBaseError(f"{root} is already open for writing by another process") from err
            try:
                yield cls(root, read_records(root), clock)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    @property
    def records(self) -> dict[str, Record]:
        return dict(self._records)

    def allocate(self, kind: str) -> str:
        """A fresh ID for a record the caller is about to propose (gaps from unused IDs are harmless)."""
        prefix = PREFIXES[kind]
        number = self._next[prefix]
        self._next[prefix] = number + 1
        return f"{prefix}-{number}"

    def commit(self, records: Iterable[Record], run_id: str, agent_id: str | None = None) -> CommitResult:
        result = CommitResult()
        alias: dict[str, str] = {}
        handles: dict[str, IO[str]] = {}
        try:
            for proposed in records:
                try:
                    record = _redirect(proposed, alias)
                except ValueError as err:  # e.g. the redirect turned the relation into a self-loop
                    result.rejected[proposed.id] = [f"after redirecting duplicate claims: {err}"]
                    self._append(
                        handles,
                        self._event(
                            "rejected", run_id, agent_id, record_id=proposed.id, detail=result.rejected[proposed.id][0]
                        ),
                    )
                    continue
                if isinstance(record, Claim) and record.canonical_hash in self._by_hash:
                    existing = self._by_hash[record.canonical_hash]
                    alias[record.id] = existing
                    result.seen_again[record.id] = existing
                    self._append(handles, self._event("seen_again", run_id, agent_id, record_id=existing))
                    continue
                reasons = problems(record, self._records)
                if reasons:
                    result.rejected[record.id] = reasons
                    self._append(
                        handles,
                        self._event("rejected", run_id, agent_id, record_id=record.id, detail="; ".join(reasons)),
                    )
                    continue
                self._append(handles, record)
                if isinstance(record, Claim):
                    self._by_hash[record.canonical_hash] = record.id
                result.written.append(record.id)
                self._append(handles, self._event("written", run_id, agent_id, record_id=record.id))
        finally:
            for handle in handles.values():
                handle.flush()
                os.fsync(handle.fileno())
                handle.close()
        return result

    def _event(self, kind: str, run_id: str, agent_id: str | None, **fields: str) -> Event:
        return Event(
            id=self.allocate("event"), event=kind, run_id=run_id, agent_id=agent_id, created_at=self._clock(), **fields
        )

    def _append(self, handles: dict[str, IO[str]], record: Record) -> None:
        name = FILES[type(record)]
        if name not in handles:
            handles[name] = open(self.root / name, "a", encoding="utf-8")
        handles[name].write(record.model_dump_json() + "\n")
        self._records[record.id] = record
        number = int(record.id.split("-")[1])
        prefix = record.id.split("-")[0]
        self._next[prefix] = max(self._next[prefix], number + 1)


_CLAIM_ID = re.compile(r"^C-\d+$")


def _redirect(record: Record, alias: dict[str, str]) -> Record:
    """Point references to a claim that turned out to be a duplicate at the existing claim."""
    if not alias:
        return record
    if isinstance(record, Relation):
        update = {
            f: alias[v] for f in ("source_id", "target_id") if _CLAIM_ID.match(v := getattr(record, f)) and v in alias
        }
        # Re-validated, not model_copy: the redirected relation must pass the per-record rules again.
        return Relation.model_validate({**record.model_dump(), **update}) if update else record
    return record
