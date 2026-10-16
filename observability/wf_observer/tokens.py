"""Token usage aggregation from workflow transcripts and orchestrator stream.

Reads agent transcripts and orchestrator stream, aggregates usage per role and agent,
applies costs, and enforces limits.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any


@dataclass(frozen=True)
class UsageRecord:
    """One deduplicated usage record from transcript or orchestrator."""

    agent_id: str
    label: str
    agent_type: str
    phase: str
    model: str
    request_id: str
    timestamp: str
    input_tokens: int
    cache_creation_5m: int
    cache_creation_1h: int
    cache_read: int
    output_tokens: int
    speed: str = "standard"  # usage.speed; only "standard" requests are priced
    workflow_run_id: str = ""  # Workflow runId the agent belongs to; "" for the orchestrator session

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


DEFAULT_CONFIG_RELPATH = Path("configs/tokens.yaml")
SCHEMA_VERSION = 3
PRICED_SPEED = "standard"
MULTIPLIER_KEYS = ("cache_read", "cache_write_5m", "cache_write_1h")
# Used only when aggregate() is called without a validated config (tests); load_tokens_config requires all three.
DEFAULT_MULTIPLIERS = {"cache_read": 0.1, "cache_write_5m": 1.25, "cache_write_1h": 2.0}
# Integer fields of an API `Usage` object read here. The SDK types the cache fields as Optional[int]:
# null means "not reported" and counts as 0. Any other non-int value is a malformed record.
USAGE_INT_FIELDS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0


def validate_tokens_config(config: dict[str, Any]) -> None:
    """Fail loudly on a price table that would silently mis-price usage."""
    prices = config.get("prices_usd_per_mtok")
    if not isinstance(prices, dict):
        raise ValueError("tokens config: 'prices_usd_per_mtok' must be a mapping")
    multipliers = prices.get("multipliers")
    if not isinstance(multipliers, dict) or any(not _is_number(multipliers.get(k)) for k in MULTIPLIER_KEYS):
        raise ValueError(f"tokens config: 'multipliers' must define non-negative numbers for {list(MULTIPLIER_KEYS)}")
    for model, model_prices in (prices.get("models") or {}).items():
        if not isinstance(model_prices, dict) or any(not _is_number(model_prices.get(k)) for k in ("input", "output")):
            raise ValueError(f"tokens config: model {model!r} must define non-negative numeric 'input' and 'output'")
    limits = config.get("limits") or {}
    for key in ("max_input_tokens", "max_output_tokens"):
        value = limits.get(key)
        if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value <= 0):
            raise ValueError(f"tokens config: limits.{key} must be null or a positive integer")


def load_tokens_config(config_file: Path | None = None) -> dict[str, Any]:
    """Load tokens.yaml. Raises FileNotFoundError if not found: silent fallback is not allowed."""
    candidates = (
        [config_file]
        if config_file
        else [
            Path(__file__).resolve().parents[1] / DEFAULT_CONFIG_RELPATH,
            Path.cwd() / DEFAULT_CONFIG_RELPATH,
        ]
    )
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            import yaml

            with open(candidate) as f:
                config = yaml.safe_load(f) or {}
            validate_tokens_config(config)
            return config
    raise FileNotFoundError(f"tokens config not found (tried: {[str(c) for c in candidates]})")


@dataclass(frozen=True)
class WorkflowLaunch:
    """One Workflow tool launch, as reported by the structured `tool_use_result` of the orchestrator stream."""

    stream: str
    session_id: str
    run_id: str
    task_id: str
    workflow_name: str
    transcript_dir: Path


def workflow_launches(stream_path: Path) -> list[WorkflowLaunch]:
    """Every workflow launched in this stream, in launch order, deduplicated by runId.

    Only the structured `tool_use_result` of the Workflow tool is trusted, never the tool_result text.
    Claude Code stores a workflow's agent transcripts in `<session_id>/subagents/workflows/<runId>/`;
    a launch whose transcriptDir does not have that shape cannot be linked and raises.
    """
    launches: dict[str, WorkflowLaunch] = {}
    with open(stream_path) as f:
        for line_num, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue  # read_orchestrator validates every line; launches only need the well-formed ones
            result = data.get("tool_use_result") if isinstance(data, dict) else None
            if not isinstance(result, dict) or not result.get("transcriptDir"):
                continue
            where = f"{stream_path}:{line_num}"
            fields = {key: result.get(key) for key in ("runId", "taskId", "workflowName", "transcriptDir")}
            fields["session_id"] = data.get("session_id")
            missing = [key for key, value in fields.items() if not isinstance(value, str) or not value]
            if missing:
                raise ValueError(f"Workflow launch without {missing} in {where}")
            transcript_dir = Path(fields["transcriptDir"])
            if (
                transcript_dir.name != fields["runId"]
                or transcript_dir.parent.parts[-2:] != ("subagents", "workflows")
                or transcript_dir.parents[2].name != fields["session_id"]
            ):
                raise ValueError(
                    f"transcriptDir {transcript_dir} is not <session {fields['session_id']}>/subagents/workflows/"
                    f"<run {fields['runId']}> in {where}"
                )
            launches.setdefault(
                fields["runId"],
                WorkflowLaunch(
                    stream=Path(stream_path).name,
                    session_id=fields["session_id"],
                    run_id=fields["runId"],
                    task_id=fields["taskId"],
                    workflow_name=fields["workflowName"],
                    transcript_dir=transcript_dir,
                ),
            )
    return list(launches.values())


def read_journal(transcript_dir: Path) -> dict[str, dict[str, Any]]:
    """Agents the workflow started, from its journal: agentId -> {label, phase, finished}, in start order.

    The journal is appended while the workflow runs, so only a partial last line is tolerated.
    """
    journal_file = transcript_dir / "journal.jsonl"
    if not journal_file.exists():
        return {}
    agents: dict[str, dict[str, Any]] = {}
    lines = journal_file.read_text().splitlines()
    for line_num, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            if line_num == len(lines):
                break  # still being written
            raise ValueError(f"Malformed JSON in {journal_file}:{line_num}: {exc}") from exc
        agent_id = event.get("agentId") if isinstance(event, dict) else None
        if not isinstance(agent_id, str) or not agent_id:
            continue
        entry = agents.setdefault(agent_id, {"label": None, "phase": None, "finished": False})
        if event.get("type") == "started":
            entry["label"], entry["phase"] = event.get("label"), event.get("phase")
        elif event.get("type") == "result":
            entry["finished"] = True
    return agents


def _token_count(container: dict[str, Any], key: str, where: str) -> int:
    """A non-negative token count; missing or null is 0, anything else non-int raises."""
    value = container.get(key)
    if value is None:
        return 0
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"Invalid usage.{key}={value!r} in {where}")
    return value


def _cache_writes(usage: dict[str, Any], where: str = "usage") -> tuple[int, int]:
    """(5m, 1h) cache-write tokens of one API usage record.

    The API reports the split under `cache_creation.ephemeral_{5m,1h}_input_tokens`; when the split is
    absent, `cache_creation_input_tokens` is the total and is counted as 5m, the default cache TTL.
    """
    split = usage.get("cache_creation")
    if isinstance(split, dict):
        return (
            _token_count(split, "ephemeral_5m_input_tokens", where),
            _token_count(split, "ephemeral_1h_input_tokens", where),
        )
    if split is not None:
        raise ValueError(f"Invalid usage.cache_creation={split!r} in {where}")
    return _token_count(usage, "cache_creation_input_tokens", where), 0


def _usage_record(
    usage: Any,
    where: str,
    *,
    agent_id: str,
    label: str,
    agent_type: str,
    phase: str,
    model: str,
    request_id: str,
    timestamp: Any,
    workflow_run_id: str = "",
) -> UsageRecord:
    """Validate one API `Usage` dict and build its record. Unknown usage fields are ignored, as the SDK does."""
    if not isinstance(usage, dict):
        raise ValueError(f"Invalid usage={usage!r} in {where}")
    counts = {key: _token_count(usage, key, where) for key in USAGE_INT_FIELDS}
    cache_creation_5m, cache_creation_1h = _cache_writes(usage, where)
    speed = usage.get("speed")
    if speed is None:
        speed = PRICED_SPEED  # older records predate the field
    elif not isinstance(speed, str) or not speed:
        raise ValueError(f"Invalid usage.speed={speed!r} in {where}")
    return UsageRecord(
        agent_id=agent_id,
        label=label,
        agent_type=agent_type,
        phase=phase,
        model=model,
        request_id=request_id,
        timestamp=timestamp if isinstance(timestamp, str) else "",
        input_tokens=counts["input_tokens"],
        cache_creation_5m=cache_creation_5m,
        cache_creation_1h=cache_creation_1h,
        cache_read=counts["cache_read_input_tokens"],
        output_tokens=counts["output_tokens"],
        speed=speed,
        workflow_run_id=workflow_run_id,
    )


def _sort_key(record: "UsageRecord") -> tuple[str, str, str]:
    """Total order on records, so aggregation does not depend on file-system iteration order."""
    return record.timestamp, record.agent_id, record.request_id


def _keep_final(records: dict[str, "UsageRecord"], key: str, record: "UsageRecord") -> None:
    """Keep one record per API request: the most complete one.

    Transcripts log a streamed response as several lines with the same request id; earlier lines carry
    partial `output_tokens` and the last one (with `stop_reason`) the final count. Input fields repeat.
    """
    seen = records.get(key)
    if seen is None or (record.output_tokens, record.timestamp) >= (seen.output_tokens, seen.timestamp):
        records[key] = record


def read_transcripts(transcript_dir: Path, launch: WorkflowLaunch | None = None) -> list[UsageRecord]:
    """Read all agent transcript files and deduplicate by requestId.

    Expects:
    - agent-<agentId>.jsonl with lines containing message.usage
    - agent-<agentId>.meta.json with agentType, description, workflowPhase, model

    With `launch`, every usage line must carry the launch's sessionId and its file's agentId, and records
    are tagged with the launch's runId: a transcript that does not belong to the workflow raises.
    """
    records = {}  # request_id -> UsageRecord (dedup by request_id)
    meta = {}  # agent_id -> meta dict

    # Read metadata files first
    for meta_file in sorted(transcript_dir.glob("agent-*.meta.json")):
        try:
            with open(meta_file) as f:
                agent_meta = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Failed to parse {meta_file}: {exc}") from exc
        if not isinstance(agent_meta, dict):
            raise ValueError(f"Failed to parse {meta_file}: expected a JSON object")
        meta[meta_file.name.removeprefix("agent-").removesuffix(".meta.json")] = agent_meta

    # Read transcript files
    for transcript_file in sorted(transcript_dir.glob("agent-*.jsonl")):
        agent_id = transcript_file.stem.removeprefix("agent-")
        agent_meta = meta.get(agent_id, {})
        label = agent_meta.get("description", agent_id)
        agent_type = agent_meta.get("agentType", "unknown")
        phase = agent_meta.get("workflowPhase", "unknown")
        model = agent_meta.get("model", "unknown")

        try:
            with open(transcript_file) as f:
                for line_num, line in enumerate(f, start=1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise ValueError(f"Malformed JSON in {transcript_file}:{line_num}: {exc}") from exc

                    # Extract usage from message if present
                    message = data.get("message") if isinstance(data, dict) else None
                    usage = message.get("usage") if isinstance(message, dict) else None
                    if not usage:
                        continue

                    where = f"{transcript_file}:{line_num}"
                    if launch is not None and (
                        data.get("sessionId") != launch.session_id or data.get("agentId") != agent_id
                    ):
                        raise ValueError(
                            f"Transcript line not linked to workflow {launch.run_id} (sessionId="
                            f"{data.get('sessionId')!r}, agentId={data.get('agentId')!r}) in {where}"
                        )
                    request_id = data.get("requestId")
                    if not request_id or not isinstance(request_id, str):
                        raise ValueError(f"Missing requestId in {where}")

                    record = _usage_record(
                        usage,
                        where,
                        agent_id=agent_id,
                        label=label,
                        agent_type=agent_type,
                        phase=phase,
                        model=model,
                        request_id=request_id,
                        timestamp=data.get("timestamp"),
                        workflow_run_id=launch.run_id if launch else "",
                    )
                    _keep_final(records, request_id, record)
        except (OSError, ValueError) as exc:
            if isinstance(exc, ValueError):
                raise
            raise ValueError(f"Failed to read {transcript_file}: {exc}") from exc

    return sorted(records.values(), key=_sort_key)


def read_orchestrator(stream_path: Path) -> list[UsageRecord]:
    """Read orchestrator usage from stream.jsonl (type:assistant lines)."""
    records = {}  # message_id -> UsageRecord

    try:
        with open(stream_path) as f:
            for line_num, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"Malformed JSON in {stream_path}:{line_num}: {exc}") from exc

                # Only process assistant (orchestrator) messages
                if not isinstance(data, dict) or data.get("type") != "assistant":
                    continue

                message = data.get("message")
                usage = message.get("usage") if isinstance(message, dict) else None
                if not usage:
                    continue

                where = f"{stream_path}:{line_num}"
                message_id = message.get("id")
                if not message_id or not isinstance(message_id, str):
                    # Usage without an id cannot be deduplicated: counting it could double-count.
                    raise ValueError(f"Missing message.id in {where}")

                record = _usage_record(
                    usage,
                    where,
                    agent_id="orchestrator",
                    label="orchestrator",
                    agent_type="orchestrator",
                    phase="orchestration",
                    model=message.get("model") or data.get("model") or "unknown",
                    request_id=message_id,
                    timestamp=data.get("timestamp"),
                )
                _keep_final(records, message_id, record)
    except (OSError, ValueError) as exc:
        if isinstance(exc, ValueError):
            raise
        raise ValueError(f"Failed to read {stream_path}: {exc}") from exc

    return sorted(records.values(), key=_sort_key)


def read_workflow(launch: WorkflowLaunch) -> tuple[list[UsageRecord], dict[str, Any]]:
    """Usage records of one workflow's agents, and the manifest entry that links them to it.

    Members are the agents in the workflow's journal plus any agent transcript in its runId directory.
    state: "ok"; "incomplete" (a journal agent has no transcript); "no_agents" (the workflow started no
    agent); "missing" (the transcript dir is gone, e.g. Claude Code deleted the session).
    """
    entry: dict[str, Any] = {
        "run_id": launch.run_id,
        "workflow_name": launch.workflow_name,
        "task_id": launch.task_id,
        "session_id": launch.session_id,
        "stream": launch.stream,
        "transcript_dir": str(launch.transcript_dir),
        "agents": [],
    }
    if not launch.transcript_dir.is_dir():
        return [], {**entry, "state": "missing"}

    journal = read_journal(launch.transcript_dir)
    with_transcript = {f.stem.removeprefix("agent-") for f in launch.transcript_dir.glob("agent-*.jsonl")}
    records = read_transcripts(launch.transcript_dir, launch)
    requests: dict[str, int] = {}
    for record in records:
        requests[record.agent_id] = requests.get(record.agent_id, 0) + 1

    for agent_id in [*journal, *sorted(with_transcript - journal.keys())]:
        meta_file = launch.transcript_dir / f"agent-{agent_id}.meta.json"
        meta = json.loads(meta_file.read_text()) if meta_file.exists() else {}
        entry["agents"].append(
            {
                "agent_id": agent_id,
                "label": (journal.get(agent_id) or {}).get("label") or meta.get("description"),
                "phase": (journal.get(agent_id) or {}).get("phase") or meta.get("workflowPhase"),
                "agent_type": meta.get("agentType"),
                "model": meta.get("model"),
                "in_journal": agent_id in journal,
                "finished": (journal.get(agent_id) or {}).get("finished", False),
                "transcript": agent_id in with_transcript,
                "requests": requests.get(agent_id, 0),
            }
        )
    if not entry["agents"]:
        state = "no_agents"
    elif any(not agent["transcript"] for agent in entry["agents"]):
        state = "incomplete"
    else:
        state = "ok"
    return records, {**entry, "state": state}


def collect_usage(stream_files: list[Path]) -> tuple[list[UsageRecord], list[dict[str, Any]]]:
    """Deduplicated usage records of the orchestrator streams and of every workflow they launched.

    Returns (records, workflows): `workflows` is the manifest linking each agent to its workflow runId,
    with one {"stream", "state": "no_launch"} entry per existing stream that launched no workflow.
    """
    records_by_request_id: dict[str, UsageRecord] = {}
    workflows: list[dict[str, Any]] = []
    seen_runs: set[str] = set()

    for stream_path in map(Path, stream_files):
        if not stream_path.exists():
            continue
        launches = workflow_launches(stream_path)
        if not launches:
            workflows.append({"stream": stream_path.name, "run_id": None, "state": "no_launch", "agents": []})
        for launch in launches:
            if launch.run_id in seen_runs:  # two streams reporting the same workflow
                continue
            seen_runs.add(launch.run_id)
            workflow_records, entry = read_workflow(launch)
            workflows.append(entry)
            for record in workflow_records:
                _keep_final(records_by_request_id, record.request_id, record)

        for record in read_orchestrator(stream_path):
            _keep_final(records_by_request_id, record.request_id, record)

    return sorted(records_by_request_id.values(), key=_sort_key), workflows


def collect_records(stream_files: list[Path]) -> list[UsageRecord]:
    """Records only; see collect_usage."""
    return collect_usage(stream_files)[0]


def role_of(agent_type: str, resolved_config: dict[str, Any]) -> str:
    """Invert agents config to find the role name for an agent_type.

    Falls back to agent_type if not found in config.
    """
    agents = resolved_config.get("agents", {})
    for role, config in agents.items():
        if config.get("agent_type") == agent_type:
            return role
    return agent_type


def aggregate(
    records: list[UsageRecord],
    limits: dict[str, int | None],
    prices: dict[str, Any],
    resolved_config: dict[str, Any] | None = None,
    *,
    source: str = "live",
    status: str = "running",
) -> dict[str, Any]:
    """Aggregate records into totals, by_role, by_agent, and series.

    Returns a dict with schema_version 3:
    - totals, by_role[r], by_agent[a]:
      - input: {fresh, cache_write, cache_read, total}: raw ints
      - input_billed_equivalent: float (weighted by cache multipliers, used for USD)
      - output: int
      - est_usd: float (always numeric, never null; 0.0 if no priced models)
    - by_agent[a] also has priced: bool
    - Top level:
      - schema_version: 3, unpriced_models: [sorted str], unpriced_tokens: int (raw)
        A request is unpriced when its model is not in the price table, or when it did not run at
        standard speed (e.g. fast mode, billed at a premium the table does not hold). The latter is
        listed as "<model> (speed=<speed>)".
      - limits: computed on raw totals
      - status: caller's status, overridden to cap_exceeded if any limit exceeded
      - source, generated_at
      - pricing: {source, priced_speed, multipliers} actually applied, so a snapshot documents its own
        estimate: per request, USD = (fresh + cache_write_5m*m5 + cache_write_1h*m1h + cache_read*mr)
        * input_price / 1e6 + output * output_price / 1e6
    - series: one point per request, in time order, all raw tokens:
      {timestamp, role, agent_id, request_id, input, output, cumulative_input, cumulative_output}
      `input`/`output` belong to that request; `cumulative_*` are running totals of its role.
    """
    multipliers = {**DEFAULT_MULTIPLIERS, **prices.get("multipliers", {})}
    pricing = {"source": prices.get("source"), "priced_speed": PRICED_SPEED, "multipliers": multipliers}
    if not records:
        return {
            "schema_version": SCHEMA_VERSION,
            "totals": {
                "input": {"fresh": 0, "cache_write": 0, "cache_read": 0, "total": 0},
                "input_billed_equivalent": 0.0,
                "output": 0,
                "est_usd": 0.0,
            },
            "by_role": {},
            "by_agent": {},
            "series": [],
            "unpriced_models": [],
            "unpriced_tokens": 0,
            "pricing": pricing,
            "limits": {
                "max_input_tokens": limits.get("max_input_tokens"),
                "max_output_tokens": limits.get("max_output_tokens"),
                "used_frac_input": None,
                "used_frac_output": None,
                "exceeded": [],
            },
            "status": status,
            "source": source,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        }

    resolved_config = resolved_config or {}
    models = prices.get("models", {})

    # Sort by timestamp for series
    sorted_records = sorted(records, key=_sort_key)

    # Aggregate by role
    by_role = {}
    by_agent = {}
    unpriced_models_set = set()
    unpriced_tokens_total = 0

    for record in sorted_records:
        role = role_of(record.agent_type, resolved_config)

        # Initialize role aggregates
        if role not in by_role:
            by_role[role] = {
                "input": {"fresh": 0, "cache_write": 0, "cache_read": 0, "total": 0},
                "input_billed_equivalent": 0.0,
                "output": 0,
                "est_usd": 0.0,
            }

        # Initialize agent aggregates
        if record.agent_id not in by_agent:
            by_agent[record.agent_id] = {
                "label": record.label,
                "role": role,
                "workflow_run_id": record.workflow_run_id or None,
                "model": record.model,
                "input": {"fresh": 0, "cache_write": 0, "cache_read": 0, "total": 0},
                "input_billed_equivalent": 0.0,
                "output": 0,
                "est_usd": 0.0,
                "priced": True,  # Assume priced until we know otherwise
            }

        # Raw tokens
        fresh = record.input_tokens
        cache_write_raw = record.cache_creation_5m + record.cache_creation_1h
        cache_read_raw = record.cache_read

        # Billed equivalent tokens
        cache_write_billed = (
            record.cache_creation_5m * multipliers["cache_write_5m"]
            + record.cache_creation_1h * multipliers["cache_write_1h"]
        )
        cache_read_billed = cache_read_raw * multipliers["cache_read"]
        input_billed_equiv = fresh + cache_write_billed + cache_read_billed

        # Add raw to role and agent
        by_role[role]["input"]["fresh"] += fresh
        by_role[role]["input"]["cache_write"] += cache_write_raw
        by_role[role]["input"]["cache_read"] += cache_read_raw
        by_role[role]["input"]["total"] += fresh + cache_write_raw + cache_read_raw
        by_role[role]["input_billed_equivalent"] += input_billed_equiv
        by_role[role]["output"] += record.output_tokens

        by_agent[record.agent_id]["input"]["fresh"] += fresh
        by_agent[record.agent_id]["input"]["cache_write"] += cache_write_raw
        by_agent[record.agent_id]["input"]["cache_read"] += cache_read_raw
        by_agent[record.agent_id]["input"]["total"] += fresh + cache_write_raw + cache_read_raw
        by_agent[record.agent_id]["input_billed_equivalent"] += input_billed_equiv
        by_agent[record.agent_id]["output"] += record.output_tokens

        # Calculate cost for this record
        model_prices = models.get(record.model, {}) if record.speed == PRICED_SPEED else {}
        if model_prices:
            input_cost = input_billed_equiv * model_prices.get("input", 0) / 1_000_000
            output_cost = record.output_tokens * model_prices.get("output", 0) / 1_000_000
            record_cost = input_cost + output_cost
            by_role[role]["est_usd"] += record_cost
            by_agent[record.agent_id]["est_usd"] += record_cost
        else:
            # Model not in price table, or non-standard speed: accumulate separately
            unpriced_models_set.add(
                record.model if record.speed == PRICED_SPEED else f"{record.model} (speed={record.speed})"
            )
            by_agent[record.agent_id]["priced"] = False
            unpriced_tokens_total += fresh + cache_write_raw + cache_read_raw + record.output_tokens

    # Per-request series (raw tokens) with per-role running totals
    series = []
    cumulative: dict[str, tuple[int, int]] = {}
    for record in sorted_records:
        role = role_of(record.agent_type, resolved_config)
        input_total_raw = record.input_tokens + record.cache_creation_5m + record.cache_creation_1h + record.cache_read
        cumulative_input, cumulative_output = cumulative.get(role, (0, 0))
        cumulative_input += input_total_raw
        cumulative_output += record.output_tokens
        cumulative[role] = (cumulative_input, cumulative_output)
        series.append(
            {
                "timestamp": record.timestamp,
                "role": role,
                "agent_id": record.agent_id,
                "request_id": record.request_id,
                "input": input_total_raw,
                "output": record.output_tokens,
                "cumulative_input": cumulative_input,
                "cumulative_output": cumulative_output,
            }
        )

    # Calculate totals (raw)
    total_input_raw = sum(r["input"]["total"] for r in by_role.values())
    total_output = sum(r["output"] for r in by_role.values())
    total_cost = sum(r["est_usd"] for r in by_role.values())

    # Check limits (on raw totals)
    exceeded = []
    used_frac_input = None
    used_frac_output = None

    if limits.get("max_input_tokens"):
        used_frac_input = total_input_raw / limits["max_input_tokens"]
        if total_input_raw > limits["max_input_tokens"]:
            exceeded.append("max_input_tokens")

    if limits.get("max_output_tokens"):
        used_frac_output = total_output / limits["max_output_tokens"]
        if total_output > limits["max_output_tokens"]:
            exceeded.append("max_output_tokens")

    final_status = "cap_exceeded" if exceeded else status

    return {
        "schema_version": SCHEMA_VERSION,
        "totals": {
            "input": {
                "fresh": sum(r["input"]["fresh"] for r in by_role.values()),
                "cache_write": sum(r["input"]["cache_write"] for r in by_role.values()),
                "cache_read": sum(r["input"]["cache_read"] for r in by_role.values()),
                "total": total_input_raw,
            },
            "input_billed_equivalent": sum(r["input_billed_equivalent"] for r in by_role.values()),
            "output": total_output,
            "est_usd": total_cost,
        },
        "by_role": by_role,
        "by_agent": by_agent,
        "series": series,
        "unpriced_models": sorted(unpriced_models_set),
        "unpriced_tokens": unpriced_tokens_total,
        "pricing": pricing,
        "limits": {
            "max_input_tokens": limits.get("max_input_tokens"),
            "max_output_tokens": limits.get("max_output_tokens"),
            "used_frac_input": used_frac_input,
            "used_frac_output": used_frac_output,
            "exceeded": exceeded,
        },
        "status": final_status,
        "source": source,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
    }


def write_snapshot(
    run_dir: Path,
    data: dict[str, Any],
) -> None:
    """Write tokens.json atomically to the run directory, forcing source='saved'."""
    run_dir = Path(run_dir).resolve()
    tokens_file = run_dir / "tokens.json"

    # Force source to 'saved' (copy data, don't mutate caller's dict)
    data_copy = dict(data)
    data_copy["source"] = "saved"

    # A unique temp file per writer: run.sh and the observer may snapshot the same run concurrently.
    with NamedTemporaryFile("w", dir=run_dir, prefix=".tokens.", suffix=".tmp", delete=False) as f:
        tmp_file = Path(f.name)
        try:
            json.dump(data_copy, f, indent=2, ensure_ascii=False, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        except BaseException:
            f.close()
            tmp_file.unlink(missing_ok=True)
            raise
    os.replace(tmp_file, tokens_file)  # atomic on the same file system


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m observability.wf_observer.tokens")
    sub = p.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="check token usage and write snapshot")
    check.add_argument("run_dir", type=Path, help="run output directory")
    check.add_argument("--config-file", type=Path, help="tokens.yaml config file")
    check.add_argument("--resolved-config", type=Path, help="resolved_config.json file")
    check.add_argument(
        "--stream",
        type=Path,
        action="append",
        dest="streams",
        help="stream file (repeatable; default: run_dir/stream.jsonl)",
    )
    check.add_argument("--status", default="running", help="workflow status (default: running)")

    return p


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Exits 3 if a token limit is exceeded."""
    args = _parser().parse_args(argv)
    run_dir = Path(args.run_dir).resolve()

    try:
        # Load tokens config
        config = load_tokens_config(args.config_file if args.config_file else None)
        limits = config.get("limits", {})
        prices = config.get("prices_usd_per_mtok", {})

        # Load resolved config if available
        resolved_config = {}
        resolved_config_file = args.resolved_config or (run_dir / "resolved_config.json")
        if resolved_config_file.exists():
            with open(resolved_config_file) as f:
                resolved_config = json.load(f)

        # Determine stream files to read
        stream_files: list[Path] = []
        if args.streams:
            stream_files = args.streams
        else:
            # Default: run_dir/stream.jsonl
            default_stream = run_dir / "stream.jsonl"
            if default_stream.exists():
                stream_files = [default_stream]

        # Collect records from all streams
        records, workflows = collect_usage(stream_files)

        # Aggregate with caller-provided status and source='live' for snapshot
        result = aggregate(records, limits, prices, resolved_config, source="live", status=args.status)

        result["workflows"] = workflows

        # Write snapshot (write_snapshot forces source='saved')
        write_snapshot(run_dir, result)

        # Print totals
        totals = result["totals"]
        usd_str = f"${totals['est_usd']:.3f}"
        print(
            f"Totals: input={totals['input']['total']} "
            f"(fresh={totals['input']['fresh']}, "
            f"cache_write={totals['input']['cache_write']}, "
            f"cache_read={totals['input']['cache_read']}) "
            f"output={totals['output']} "
            f"est_usd={usd_str}"
        )

        # Print unpriced models if any
        if result.get("unpriced_models"):
            print(f"warning: unpriced models: {', '.join(result['unpriced_models'])}")
            print(f"warning: unpriced tokens: {result['unpriced_tokens']}")

        for workflow in workflows:
            if workflow["state"] != "ok":
                print(f"warning: {workflow['stream']} {workflow['run_id'] or ''}: {workflow['state']}")

        # Check limits
        if result["limits"]["exceeded"]:
            print(f"error: token limits exceeded: {result['limits']['exceeded']}")
            return 3
        return 0

    except Exception as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
