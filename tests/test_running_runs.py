"""Liveness detection behind GET /runs/running."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from observability.wf_observer.server import (
    _extract_metrics_from_outputs,
    _extract_phase_from_journals,
    _tail_jsonl,
    is_run_in_progress,
    scan_running_runs,
)

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)


def _context(minutes_ago: float) -> dict:
    started = (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"started_utc": started, "ca_timeout_s": 600, "discovery_timeout_s": 900}


def test_recent_run_without_markers_is_running(tmp_path: Path) -> None:
    assert is_run_in_progress(tmp_path, _context(5), now=NOW)


@pytest.mark.parametrize("marker", ["exit_code", "result.json", "summary.json", "summary.txt"])
def test_terminal_marker_means_finished(tmp_path: Path, marker: str) -> None:
    (tmp_path / marker).write_text("0")
    assert not is_run_in_progress(tmp_path, _context(5), now=NOW)


def test_run_past_timeout_budget_is_dead(tmp_path: Path) -> None:
    # budget = 600 + 900 + 600 grace = 35 min
    assert is_run_in_progress(tmp_path, _context(34), now=NOW)
    assert not is_run_in_progress(tmp_path, _context(36), now=NOW)


def test_missing_start_time_is_not_running(tmp_path: Path) -> None:
    assert not is_run_in_progress(tmp_path, {}, now=NOW)


# Tests for _tail_jsonl


def test_tail_jsonl_returns_empty_for_missing_file(tmp_path: Path) -> None:
    """_tail_jsonl returns [] for non-existent files."""
    result = _tail_jsonl(tmp_path / "does_not_exist.jsonl")
    assert result == []


def test_tail_jsonl_reads_small_file_entirely(tmp_path: Path) -> None:
    """_tail_jsonl reads entire small files."""
    jsonl_file = tmp_path / "small.jsonl"
    lines = [{"id": 1}, {"id": 2}, {"id": 3}]
    jsonl_file.write_text("\n".join(json.dumps(line) for line in lines))

    result = _tail_jsonl(jsonl_file)
    assert result == lines


def test_tail_jsonl_reads_tail_of_large_file(tmp_path: Path) -> None:
    """_tail_jsonl reads only the tail of large files."""
    jsonl_file = tmp_path / "large.jsonl"

    # Create a large file (>64KB) with numbered lines
    # Each line is about 50 bytes when serialized, so we need ~1300+ lines for 64KB
    large_lines = [{"seq": i, "data": "x" * 40} for i in range(2000)]
    jsonl_file.write_text("\n".join(json.dumps(line) for line in large_lines))

    # Verify file is actually larger than max_bytes
    file_size = jsonl_file.stat().st_size
    assert file_size > 64 * 1024, f"File size {file_size} is not larger than 64KB"

    # Read tail (only last 64KB)
    result = _tail_jsonl(jsonl_file, max_bytes=64 * 1024)

    # Should have some but not all lines
    assert len(result) > 0
    assert len(result) < len(large_lines)
    # Last entry should be preserved
    assert result[-1]["seq"] == 1999


def test_tail_jsonl_skips_first_partial_line(tmp_path: Path) -> None:
    """_tail_jsonl drops the first partial line after seeking."""
    jsonl_file = tmp_path / "partial.jsonl"

    # Create file with many lines so seeking will happen
    lines = [{"id": i} for i in range(1000)]
    content = "\n".join(json.dumps(line) for line in lines)
    jsonl_file.write_text(content)

    # Read with small max_bytes to force a seek
    result = _tail_jsonl(jsonl_file, max_bytes=100)

    # Result should be valid (no partial JSON at start)
    assert all(isinstance(item, dict) for item in result)


def test_tail_jsonl_skips_malformed_lines(tmp_path: Path) -> None:
    """_tail_jsonl skips malformed JSON lines."""
    jsonl_file = tmp_path / "malformed.jsonl"

    content = "\n".join(
        [
            '{"valid": 1}',
            "not valid json",
            '{"also_valid": 2}',
            "incomplete {",
            '{"valid": 3}',
        ]
    )
    jsonl_file.write_text(content)

    result = _tail_jsonl(jsonl_file)
    # Should have 3 valid objects, not 5
    assert len(result) == 3
    assert result[0] == {"valid": 1}
    assert result[1] == {"also_valid": 2}
    assert result[2] == {"valid": 3}


def test_tail_jsonl_returns_only_dicts(tmp_path: Path) -> None:
    """_tail_jsonl returns only dict objects, not arrays or primitives."""
    jsonl_file = tmp_path / "mixed.jsonl"

    content = "\n".join(
        [
            '{"object": true}',
            "[1, 2, 3]",
            '"string"',
            "123",
            '{"another": "object"}',
        ]
    )
    jsonl_file.write_text(content)

    result = _tail_jsonl(jsonl_file)
    assert len(result) == 2
    assert all(isinstance(item, dict) for item in result)


# Tests for scan_running_runs


def test_scan_running_runs_returns_empty_for_missing_root(tmp_path: Path) -> None:
    """scan_running_runs returns [] if experiments_root does not exist."""
    result = scan_running_runs(tmp_path / "does_not_exist")
    assert result == []


def test_scan_running_runs_skips_experiment_with_exit_code(tmp_path: Path) -> None:
    """scan_running_runs returns [] for experiments with exit_code marker."""
    exp_dir = tmp_path / "exp1"
    results_dir = exp_dir / "results"
    results_dir.mkdir(parents=True)

    # Write context and exit_code
    context = _context(5)
    (results_dir / "context.json").write_text(json.dumps(context))
    (results_dir / "exit_code").write_text("0")

    result = scan_running_runs(tmp_path)
    assert result == []


def test_scan_running_runs_includes_fresh_run(tmp_path: Path) -> None:
    """scan_running_runs includes a fresh run with context.json and no exit_code."""
    exp_dir = tmp_path / "exp1"
    results_dir = exp_dir / "results"
    results_dir.mkdir(parents=True)

    # Write context only (no exit_code)
    context = _context(5)
    context["run_id"] = "test-run-1"
    (results_dir / "context.json").write_text(json.dumps(context))

    result = scan_running_runs(tmp_path, now=NOW)
    assert len(result) == 1
    assert result[0]["run_id"] == "test-run-1"
    assert result[0]["type"] == "run.sh"
    assert result[0]["start_time"] == context["started_utc"]


def test_scan_running_runs_extracts_phase_from_journal(tmp_path: Path) -> None:
    """scan_running_runs extracts phase and stage from discovery-journal.jsonl."""
    exp_dir = tmp_path / "exp1"
    results_dir = exp_dir / "results"
    results_dir.mkdir(parents=True)

    # Write context
    context = _context(5)
    (results_dir / "context.json").write_text(json.dumps(context))

    # Write journal with phase event
    journal_content = json.dumps({"type": "step", "phase": "Critique", "timestamp": "2026-09-29T12:00:00Z"})
    (results_dir / "discovery-journal.jsonl").write_text(journal_content)

    result = scan_running_runs(tmp_path, now=NOW)
    assert len(result) == 1
    assert result[0]["stage"] == "Critique"
    assert result[0]["percent_complete"] == 50


def test_scan_running_runs_extracts_metrics_from_output(tmp_path: Path) -> None:
    """scan_running_runs extracts token metrics from discovery-output.jsonl."""
    exp_dir = tmp_path / "exp1"
    results_dir = exp_dir / "results"
    results_dir.mkdir(parents=True)

    # Write context
    context = _context(5)
    (results_dir / "context.json").write_text(json.dumps(context))

    # Write output with task_progress event
    output_content = json.dumps({"type": "system", "subtype": "task_progress", "usage": {"total_tokens": 5000}})
    (results_dir / "discovery-output.jsonl").write_text(output_content)

    result = scan_running_runs(tmp_path, now=NOW)
    assert len(result) == 1
    assert result[0]["tokens_so_far"] == 5000


def test_scan_running_runs_caps_at_max_results(tmp_path: Path) -> None:
    """scan_running_runs returns at most _RUNNING_MAX_RESULTS entries."""
    # Create multiple experiments
    num_exps = 25
    for i in range(num_exps):
        exp_dir = tmp_path / f"exp{i}"
        results_dir = exp_dir / "results"
        results_dir.mkdir(parents=True)

        # Each run starts at a different time (minutes ago)
        context = _context(num_exps - i)  # First one oldest, last one newest
        context["run_id"] = f"run-{i}"
        (results_dir / "context.json").write_text(json.dumps(context))

    # Patch the constant to a smaller value for testing
    with patch("observability.wf_observer.server._RUNNING_MAX_RESULTS", 2):
        result = scan_running_runs(tmp_path, now=NOW)
        # Should be capped at 2
        assert len(result) == 2
        # Most recent first
        assert result[0]["run_id"] == "run-24"
        assert result[1]["run_id"] == "run-23"


def test_scan_running_runs_skips_dead_runs(tmp_path: Path) -> None:
    """scan_running_runs skips runs that exceed their timeout budget."""
    exp_dir = tmp_path / "exp1"
    results_dir = exp_dir / "results"
    results_dir.mkdir(parents=True)

    # Write context for a run that is way past its budget (e.g., 50 minutes old)
    context = _context(50)  # 50 minutes ago
    (results_dir / "context.json").write_text(json.dumps(context))

    result = scan_running_runs(tmp_path, now=NOW)
    # Should be empty because the run is dead
    assert result == []


def test_extract_phase_from_journals_returns_none_for_empty(tmp_path: Path) -> None:
    """_extract_phase_from_journals returns None if no journal files exist."""
    result = _extract_phase_from_journals(tmp_path)
    assert result is None


def test_extract_metrics_from_outputs_returns_none_for_empty(tmp_path: Path) -> None:
    """_extract_metrics_from_outputs returns None if no output files exist."""
    result = _extract_metrics_from_outputs(tmp_path)
    assert result is None
