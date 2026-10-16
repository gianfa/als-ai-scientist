"""Run event record: one line of a run's append-only `events.jsonl`."""

from __future__ import annotations

import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

EVENT_KINDS = frozenset(
    {
        "run_started",
        "run_finished",
        "agent_started",
        "agent_finished",
        "tool_call",
        "gate",
        "error",
    }
)

RUN_ID_PATTERN = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")


class InvalidEventError(ValueError):
    """An event does not match the expected structure."""


def new_run_id(now: datetime | None = None) -> str:
    """Return a sortable run ID, e.g. `20260925T101500Z-1a2b3c4d`."""
    now = now or datetime.now(timezone.utc)
    return f"{now.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass(frozen=True)
class Event:
    run_id: str
    kind: str
    agent: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    # Measured values only; None means "not recorded", never "zero".
    latency_ms: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    timestamp: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not RUN_ID_PATTERN.match(self.run_id):
            raise InvalidEventError(f"invalid run_id: {self.run_id!r}")
        if self.kind not in EVENT_KINDS:
            raise InvalidEventError(f"unknown event kind: {self.kind!r}")
        if not isinstance(self.data, dict):
            raise InvalidEventError("data must be an object")
        for name in ("latency_ms", "input_tokens", "output_tokens", "cost_usd"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0):
                raise InvalidEventError(f"{name} must be a non-negative number, got {value!r}")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Event:
        unknown = set(raw) - {f for f in cls.__dataclass_fields__}
        if unknown:
            raise InvalidEventError(f"unknown fields: {sorted(unknown)}")
        try:
            return cls(**raw)
        except TypeError as exc:
            raise InvalidEventError(str(exc)) from exc
