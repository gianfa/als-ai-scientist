#!/usr/bin/env python3
"""Record the resolved model IDs and Claude Code version seen in stage streams.

`context.json` holds the requested model alias (`launcher_model`); an alias can point to different
models over time. The stream-json `system/init` event carries the resolved `model` and
`claude_code_version`, and each `assistant` event carries `message.model`, which also covers the
sub-agents when their messages reach the stream.

Usage:
    extract_resolved_models.py <context.json> [--prompts <prompts.jsonl>] <stage>=<stream.jsonl> ...

Sub-agent messages do not reach the launcher stream, so with --prompts the models of the sub-agents
are read from prompts.jsonl (the harness's own per-agent metadata) into `agent_models`.

A `system/model_refusal_fallback` event means the server swapped the model mid-session (e.g. a safety
classifier stopped the response and the session continued on an older model); it is recorded in
`fallbacks`, since `assistant_models` alone would just show an unexpected extra model. This cannot be
prevented from the client (`--fallback-model` only covers overload).

Adds `resolved_models` to context.json:
{stage: {init_model, claude_code_version, assistant_models, agent_models, fallbacks}}.
A missing or unreadable stream is recorded as an error string, never skipped silently.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def summarize_stream(stream: Path) -> dict[str, Any]:
    init_model: str | None = None
    version: str | None = None
    assistant_models: set[str] = set()
    fallbacks: list[dict[str, Any]] = []
    for line in stream.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "system" and event.get("subtype") == "init":
            init_model = init_model or event.get("model")
            version = version or event.get("claude_code_version")
        elif event.get("type") == "system" and event.get("subtype") == "model_refusal_fallback":
            fallbacks.append(
                {k: event.get(k) for k in ("original_model", "fallback_model", "trigger", "api_refusal_category")}
            )
        elif event.get("type") == "assistant":
            model = (event.get("message") or {}).get("model")
            if isinstance(model, str) and model != "<synthetic>":
                assistant_models.add(model)
    if init_model is None and not assistant_models:
        raise ValueError("no init or assistant model found in stream")
    return {
        "init_model": init_model,
        "claude_code_version": version,
        "assistant_models": sorted(assistant_models),
        "fallbacks": fallbacks,
    }


def agent_models_by_stage(prompts: Path) -> dict[str, list[str]]:
    """Distinct sub-agent models per stage, from the prompt archive."""
    found: dict[str, set[str]] = {}
    for line in prompts.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("model"):
            found.setdefault(str(row.get("stage")), set()).add(row["model"])
    return {stage: sorted(models) for stage, models in found.items()}


def record(context_path: Path, streams: dict[str, Path], prompts: Path | None = None) -> dict[str, Any]:
    context = json.loads(context_path.read_text(encoding="utf-8"))
    resolved = dict(context.get("resolved_models") or {})
    for stage, stream in streams.items():
        try:
            resolved[stage] = summarize_stream(stream)
        except (OSError, ValueError) as exc:
            resolved[stage] = {"error": f"{type(exc).__name__}: {exc}"}
    if prompts is not None:
        try:
            for stage, models in agent_models_by_stage(prompts).items():
                if isinstance(resolved.get(stage), dict) and "error" not in resolved[stage]:
                    resolved[stage]["agent_models"] = models
        except (OSError, json.JSONDecodeError) as exc:
            for entry in resolved.values():
                if isinstance(entry, dict):
                    entry["agent_models_error"] = f"{type(exc).__name__}: {exc}"
    context["resolved_models"] = resolved
    context_path.write_text(json.dumps(context, indent=2) + "\n", encoding="utf-8")
    return resolved


def main(argv: list[str]) -> int:
    args = argv[2:]
    prompts: Path | None = None
    if args[:1] == ["--prompts"] and len(args) >= 2:
        prompts, args = Path(args[1]), args[2:]
    if len(argv) < 3 or any("=" not in a for a in args):
        print(__doc__, file=sys.stderr)
        return 2
    streams = {a.split("=", 1)[0]: Path(a.split("=", 1)[1]) for a in args}
    resolved = record(Path(argv[1]), streams, prompts)
    return 1 if any("error" in v for v in resolved.values()) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
