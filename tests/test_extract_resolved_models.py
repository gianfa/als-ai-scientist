"""Tests for scripts/ops/extract_resolved_models.py (repository root)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load():
    path = REPO_ROOT / "scripts" / "ops" / "extract_resolved_models.py"
    spec = importlib.util.spec_from_file_location("extract_resolved_models", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _stream(path: Path, events: list) -> Path:
    path.write_text("\n".join(e if isinstance(e, str) else json.dumps(e) for e in events) + "\n")
    return path


def test_records_init_and_assistant_models(tmp_path: Path) -> None:
    mod = _load()
    ctx = tmp_path / "context.json"
    ctx.write_text(json.dumps({"run_id": "r1", "launcher_model": "sonnet"}))
    stream = _stream(
        tmp_path / "s.jsonl",
        [
            {"type": "system", "subtype": "init", "model": "claude-sonnet-5-5", "claude_code_version": "2.1.285"},
            "not json",
            {"type": "assistant", "message": {"model": "claude-sonnet-5-5"}},
            {"type": "assistant", "message": {"model": "claude-opus-5-5"}},
            {"type": "assistant", "message": {"model": "<synthetic>"}},
        ],
    )
    resolved = mod.record(ctx, {"discovery": stream})
    saved = json.loads(ctx.read_text())
    assert saved["launcher_model"] == "sonnet" and saved["resolved_models"] == resolved
    assert resolved["discovery"] == {
        "init_model": "claude-sonnet-5-5",
        "claude_code_version": "2.1.285",
        "assistant_models": ["claude-opus-5-5", "claude-sonnet-5-5"],
        "fallbacks": [],
    }


def test_missing_or_empty_stream_is_recorded_as_error(tmp_path: Path) -> None:
    mod = _load()
    ctx = tmp_path / "context.json"
    ctx.write_text("{}")
    empty = _stream(tmp_path / "e.jsonl", [{"type": "user"}])
    resolved = mod.record(ctx, {"ca": tmp_path / "missing.jsonl", "discovery": empty})
    assert "error" in resolved["ca"] and "error" in resolved["discovery"]
    assert mod.main(["x", str(ctx), f"ca={tmp_path / 'missing.jsonl'}"]) == 1


def test_agent_models_come_from_prompt_archive(tmp_path: Path) -> None:
    mod = _load()
    ctx = tmp_path / "context.json"
    ctx.write_text("{}")
    stream = _stream(tmp_path / "s.jsonl", [{"type": "system", "subtype": "init", "model": "claude-opus-5-5"}])
    prompts = _stream(
        tmp_path / "prompts.jsonl",
        [
            {"stage": "discovery", "model": "claude-sonnet-5"},
            {"stage": "discovery", "model": "claude-sonnet-5"},
            {"stage": "ca", "model": None},
        ],
    )
    resolved = mod.record(ctx, {"discovery": stream}, prompts)
    assert resolved["discovery"]["agent_models"] == ["claude-sonnet-5"]
    assert mod.main(["x", str(ctx), "--prompts", str(prompts), f"discovery={stream}"]) == 0


def test_model_refusal_fallback_is_recorded(tmp_path: Path) -> None:
    mod = _load()
    ctx = tmp_path / "context.json"
    ctx.write_text("{}")
    stream = _stream(
        tmp_path / "s.jsonl",
        [
            {"type": "system", "subtype": "init", "model": "claude-opus-5-5"},
            {
                "type": "system",
                "subtype": "model_refusal_fallback",
                "trigger": "refusal",
                "original_model": "claude-opus-5-5",
                "fallback_model": "claude-opus-5",
                "api_refusal_category": "bio",
            },
            {"type": "assistant", "message": {"model": "claude-opus-5"}},
        ],
    )
    resolved = mod.record(ctx, {"ca": stream})
    assert resolved["ca"]["fallbacks"] == [
        {
            "original_model": "claude-opus-5-5",
            "fallback_model": "claude-opus-5",
            "trigger": "refusal",
            "api_refusal_category": "bio",
        }
    ]
