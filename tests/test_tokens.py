import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from observability.wf_observer.server import make_handler
from observability.wf_observer.store import RunStore
from observability.wf_observer.tokens import (
    UsageRecord,
    aggregate,
    collect_records,
    collect_usage,
    load_tokens_config,
    read_orchestrator,
    read_transcripts,
    validate_tokens_config,
    workflow_launches,
    write_snapshot,
)

SESSION_ID = "sess-1"
RUN_ID = "wf_test-001"


def _workflow_dir(root: Path, session_id: str = SESSION_ID, run_id: str = RUN_ID) -> Path:
    """A transcript dir laid out as Claude Code does: <session>/subagents/workflows/<runId>."""
    tr_dir = root / session_id / "subagents" / "workflows" / run_id
    tr_dir.mkdir(parents=True)
    return tr_dir


def _launch_line(tr_dir: Path, session_id: str = SESSION_ID, run_id: str = RUN_ID) -> dict:
    return {
        "type": "user",
        "session_id": session_id,
        "tool_use_result": {
            "status": "async_launched",
            "taskType": "local_workflow",
            "taskId": "task-1",
            "workflowName": "als-discovery",
            "runId": run_id,
            "transcriptDir": str(tr_dir),
        },
    }


@pytest.fixture
def tmp_transcript_dir(tmp_path: Path) -> Path:
    """Create a minimal transcript directory with meta and transcript files."""
    tr_dir = _workflow_dir(tmp_path)

    # Agent metadata
    meta = {
        "agentType": "als-evidence",
        "description": "Evidence Agent #1",
        "workflowPhase": "evidence",
        "model": "claude-sonnet-5",
    }
    (tr_dir / "agent-agent-1.meta.json").write_text(json.dumps(meta))

    # Transcript with a request logged twice (streaming): deduplicated to its complete line
    transcript_lines = [
        {
            "requestId": "req-001",
            "sessionId": SESSION_ID,
            "agentId": "agent-1",
            "timestamp": "2026-09-26T12:00:00.000Z",
            "message": {
                "usage": {
                    "input_tokens": 100,
                    "cache_creation": {"ephemeral_5m_input_tokens": 10, "ephemeral_1h_input_tokens": 0},
                    "cache_read_input_tokens": 5,
                    "output_tokens": 50,
                }
            },
        },
        {
            # Same request logged again with a partial streaming count: the complete line (output 50) wins.
            "requestId": "req-001",
            "sessionId": SESSION_ID,
            "agentId": "agent-1",
            "timestamp": "2026-09-26T12:00:01.000Z",
            "message": {
                "usage": {
                    "input_tokens": 100,
                    "cache_creation": {"ephemeral_5m_input_tokens": 10, "ephemeral_1h_input_tokens": 0},
                    "cache_read_input_tokens": 5,
                    "output_tokens": 1,
                }
            },
        },
        {
            "requestId": "req-002",
            "sessionId": SESSION_ID,
            "agentId": "agent-1",
            "timestamp": "2026-09-26T12:00:02.000Z",
            "message": {
                "usage": {
                    "input_tokens": 200,
                    "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 20},
                    "cache_read_input_tokens": 10,
                    "output_tokens": 75,
                }
            },
        },
    ]
    (tr_dir / "agent-agent-1.jsonl").write_text("\n".join(json.dumps(line) for line in transcript_lines))

    return tr_dir


@pytest.fixture
def tmp_stream_file(tmp_path: Path, tmp_transcript_dir: Path) -> Path:
    """Create a minimal stream.jsonl with orchestrator records and transcript dir pointer."""
    stream = tmp_path / "stream.jsonl"
    lines = [
        json.dumps({"type": "start", "timestamp": "2026-09-26T12:00:00.000Z"}),
        json.dumps(
            {
                "type": "assistant",
                "timestamp": "2026-09-26T12:00:03.000Z",
                "message": {
                    "id": "msg-001",
                    "model": "claude-opus-5-5",
                    "usage": {
                        "input_tokens": 50,
                        "cache_creation": {"ephemeral_5m_input_tokens": 5, "ephemeral_1h_input_tokens": 0},
                        "cache_read_input_tokens": 2,
                        "output_tokens": 100,
                    },
                },
            }
        ),
        json.dumps(
            {
                "type": "assistant",
                "timestamp": "2026-09-26T12:00:04.000Z",
                "message": {
                    "id": "msg-002",
                    "model": "claude-opus-5-5",
                    "usage": {
                        "input_tokens": 75,
                        "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 8},
                        "cache_read_input_tokens": 3,
                        "output_tokens": 120,
                    },
                },
            }
        ),
        json.dumps(_launch_line(tmp_transcript_dir)),
    ]
    stream.write_text("\n".join(json.dumps(json.loads(line)) for line in lines))
    return stream


def test_workflow_launches_reads_structured_launch(tmp_stream_file: Path, tmp_transcript_dir: Path) -> None:
    [launch] = workflow_launches(tmp_stream_file)
    assert (launch.run_id, launch.task_id, launch.workflow_name, launch.session_id, launch.transcript_dir) == (
        RUN_ID,
        "task-1",
        "als-discovery",
        SESSION_ID,
        tmp_transcript_dir,
    )


def test_read_transcripts_deduplicates(tmp_transcript_dir: Path) -> None:
    """Test that duplicate requestIds are deduplicated."""
    records = read_transcripts(tmp_transcript_dir)
    assert len(records) == 2, "Should have 2 unique records (req-001 and req-002)"
    assert records[0].request_id == "req-001"
    assert records[1].request_id == "req-002"
    # Verify that the second occurrence of req-001 was skipped (not the 999 values)
    assert records[0].input_tokens == 100
    assert records[0].output_tokens == 50


def test_read_transcripts_extracts_usage(tmp_transcript_dir: Path) -> None:
    """Test that usage records are correctly extracted."""
    records = read_transcripts(tmp_transcript_dir)
    assert len(records) == 2
    r1 = records[0]
    assert r1.input_tokens == 100
    assert r1.cache_creation_5m == 10
    assert r1.cache_creation_1h == 0
    assert r1.cache_read == 5
    assert r1.output_tokens == 50
    assert r1.label == "Evidence Agent #1"
    assert r1.agent_type == "als-evidence"
    assert r1.phase == "evidence"
    assert r1.model == "claude-sonnet-5"


def test_read_orchestrator(tmp_stream_file: Path) -> None:
    """Test reading orchestrator records from stream.jsonl."""
    records = read_orchestrator(tmp_stream_file)
    assert len(records) == 2
    assert all(r.agent_id == "orchestrator" for r in records)
    assert records[0].input_tokens == 50
    assert records[0].output_tokens == 100
    assert records[1].input_tokens == 75
    assert records[1].output_tokens == 120


def test_aggregate_calculates_totals(tmp_transcript_dir: Path, tmp_stream_file: Path) -> None:
    """Test that aggregation calculates correct raw totals."""
    records = read_transcripts(tmp_transcript_dir) + read_orchestrator(tmp_stream_file)
    limits = {"max_input_tokens": 1000, "max_output_tokens": 500}
    prices = {"models": {}, "multipliers": {"cache_write_5m": 1.25, "cache_write_1h": 2.0, "cache_read": 0.1}}
    result = aggregate(records, limits, prices)

    # Transcripts: req-001 (100+10+0+5=115 raw), req-002 (200+0+20+10=230 raw)
    # Orchestrator: msg-001 (50+5+0+2=57 raw), msg-002 (75+0+8+3=86 raw)
    # Fresh: 100 + 200 + 50 + 75 = 425
    # Cache write (raw): 10 + 20 + 5 + 8 = 43
    # Cache read (raw): 5 + 10 + 2 + 3 = 20
    # Output: 50 + 75 + 100 + 120 = 345
    assert result["totals"]["input"]["fresh"] == 100 + 200 + 50 + 75
    assert result["totals"]["input"]["cache_write"] == 10 + 20 + 5 + 8
    assert result["totals"]["input"]["cache_read"] == 5 + 10 + 2 + 3
    assert result["totals"]["input"]["total"] == 425 + 43 + 20
    assert result["totals"]["output"] == 50 + 75 + 100 + 120
    assert result["schema_version"] == 3


def test_aggregate_checks_limits(tmp_transcript_dir: Path, tmp_stream_file: Path) -> None:
    """Test that aggregate detects when limits are exceeded (on raw tokens)."""
    records = read_transcripts(tmp_transcript_dir) + read_orchestrator(tmp_stream_file)
    limits = {"max_input_tokens": 100, "max_output_tokens": 100}  # Very low limits (raw)
    prices = {"models": {}, "multipliers": {}}
    result = aggregate(records, limits, prices)

    # Total raw input is 425+43+20=488, output is 345, both exceed limits
    assert "max_input_tokens" in result["limits"]["exceeded"]
    assert "max_output_tokens" in result["limits"]["exceeded"]
    assert result["status"] == "cap_exceeded"


def test_aggregate_with_prices(tmp_transcript_dir: Path) -> None:
    """Test cost calculation with price table."""
    records = read_transcripts(tmp_transcript_dir)
    limits = {}
    prices = {
        "models": {
            "claude-sonnet-5": {"input": 2, "output": 10},  # $2/MTok input, $10/MTok output
        },
        "multipliers": {"cache_write_5m": 1.25, "cache_write_1h": 2.0, "cache_read": 0.1},
    }
    result = aggregate(records, limits, prices)

    # Verify cost is always numeric (never None) and > 0 for priced models
    assert isinstance(result["totals"]["est_usd"], float)
    assert result["totals"]["est_usd"] > 0
    assert result["unpriced_models"] == []
    assert result["unpriced_tokens"] == 0


def test_write_snapshot(tmp_path: Path) -> None:
    """Test atomic snapshot writing and source forced to 'saved'."""
    data = {
        "schema_version": 3,
        "totals": {"input": {"total": 100}, "output": 50},
        "source": "live",  # Should be overridden
    }
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    write_snapshot(run_dir, data)
    tokens_file = run_dir / "tokens.json"
    assert tokens_file.exists()
    written_data = json.loads(tokens_file.read_text())
    assert written_data["source"] == "saved"
    assert written_data["schema_version"] == 3
    # Caller's dict should be unaffected
    assert data["source"] == "live"


def test_server_tokens_list(tmp_path: Path) -> None:
    """Test /tokens endpoint lists smoke runs."""
    store = RunStore(tmp_path / "runs")
    smoke_root = tmp_path / "smoke"
    smoke_root.mkdir()
    e2e_root = tmp_path / "e2e"  # Empty e2e root to avoid picking up real runs
    e2e_root.mkdir()

    # Create fake smoke run directories (format: YYYYMMDDTHHMMSSz with optional -hex suffix)
    run1 = smoke_root / "20260926T120000Z"
    run1.mkdir()
    (run1 / "summary.json").write_text(json.dumps({"workflow_status": "completed"}))
    (run1 / "tokens.json").write_text(json.dumps({"totals": {}}))

    run2 = smoke_root / "20260926T120100Z"
    run2.mkdir()
    (run2 / "summary.json").write_text(json.dumps({"workflow_status": "running"}))
    (run2 / "stream.jsonl").write_text("")  # Has live data

    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        make_handler(store, smoke_root=smoke_root, e2e_root=e2e_root),
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    try:
        with urllib.request.urlopen(f"{base}/tokens") as r:
            data = json.load(r)
            assert len(data) == 2
            # Both should have kind="smoke"
            assert all(r["kind"] == "smoke" for r in data)
            assert data[0]["run_id"] == "20260926T120100Z"  # Newest first
            assert data[0]["has_live"] is True
            assert data[0]["has_saved"] is False
            assert data[0]["finished"] is False
            assert data[1]["has_saved"] is True
            assert data[1]["finished"] is True
    finally:
        server.shutdown()
        server.server_close()


def test_server_tokens_saved_mode(tmp_path: Path) -> None:
    """Test /tokens/<run>?mode=saved returns snapshot with legacy flag if no schema_version."""
    store = RunStore(tmp_path / "runs")
    smoke_root = tmp_path / "smoke"
    smoke_root.mkdir()
    e2e_root = tmp_path / "e2e"
    e2e_root.mkdir()

    run_id = "20260926T120000Z"
    run_dir = smoke_root / run_id
    run_dir.mkdir()
    token_data = {"totals": {"input": {"total": 100}, "output": 50}, "status": "running"}
    (run_dir / "tokens.json").write_text(json.dumps(token_data))

    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        make_handler(store, smoke_root=smoke_root, e2e_root=e2e_root),
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    try:
        with urllib.request.urlopen(f"{base}/tokens/{run_id}?mode=saved") as r:
            data = json.load(r)
            # Legacy flag is added to response when schema_version is missing
            assert data["legacy"] is True
            # The original data is intact, we just add the flag
            assert data["totals"] == token_data["totals"]
            assert data["status"] == token_data["status"]
    finally:
        server.shutdown()
        server.server_close()


def test_server_tokens_invalid_run_id_gives_404(tmp_path: Path) -> None:
    """Test that invalid run IDs are rejected (path traversal prevention)."""
    store = RunStore(tmp_path / "runs")
    smoke_root = tmp_path / "smoke"
    smoke_root.mkdir()

    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        make_handler(store, smoke_root=smoke_root),
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(f"{base}/tokens/..%2F..%2Fetc")
        assert exc.value.code == 404
    finally:
        server.shutdown()
        server.server_close()


def test_server_static_chart_js(tmp_path: Path) -> None:
    """Test /static/chart.umd.min.js is served."""
    store = RunStore(tmp_path / "runs")
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        make_handler(store),
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    try:
        with urllib.request.urlopen(f"{base}/static/chart.umd.min.js") as r:
            assert r.headers["Content-Type"] == "application/javascript; charset=utf-8"
            content = r.read()
            assert b"Chart" in content
    finally:
        server.shutdown()
        server.server_close()


def test_server_static_other_file_gives_404(tmp_path: Path) -> None:
    """Test that non-whitelisted static files are rejected."""
    store = RunStore(tmp_path / "runs")
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        make_handler(store),
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(f"{base}/static/whatever.js")
        assert exc.value.code == 404
    finally:
        server.shutdown()
        server.server_close()


def test_server_serves_the_ui_and_its_token_module(tmp_path: Path) -> None:
    """The page is markup plus ES modules, so the tokens view is a served file.

    Asserting only on `ui.html` would pass on a page whose modules 404, which is precisely how the
    split could break without any test noticing.
    """
    store = RunStore(tmp_path / "runs")
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        make_handler(store),
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    try:
        with urllib.request.urlopen(f"{base}/") as r:
            page = r.read().decode()
        assert 'data-tab="run"' in page and 'data-tab="agents"' not in page
        assert "/static/js/main.js" in page

        for module in ("js/main.js", "js/state.js", "js/run/tokens.js"):
            with urllib.request.urlopen(f"{base}/static/{module}") as r:
                assert r.status == 200
                assert r.headers["Content-Type"].startswith("application/javascript")
                body = r.read().decode()
            if module.endswith("tokens.js"):
                assert "token-bar" in body
    finally:
        server.shutdown()
        server.server_close()


def test_static_refuses_paths_outside_the_asset_tree(tmp_path: Path) -> None:
    """The module path comes from the URL, so traversal must not escape STATIC_DIR."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(RunStore(tmp_path / "runs")))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        for path in ("js/../../server.py", "..%2F..%2Fserver.py", "js/main.py", "js"):
            with pytest.raises(urllib.error.HTTPError) as exc:
                urllib.request.urlopen(f"{base}/static/{path}")
            assert exc.value.code == 404
    finally:
        server.shutdown()
        server.server_close()


def test_aggregate_mixed_priced_unpriced(tmp_path: Path) -> None:
    """Test mixed priced/unpriced sequence: no exception, numeric est_usd, unpriced tracked."""
    tr_dir = tmp_path / "transcripts"
    tr_dir.mkdir()

    # Two agents: one priced, one unpriced
    meta_priced = {
        "agentType": "als-evidence",
        "description": "Priced Agent",
        "workflowPhase": "evidence",
        "model": "claude-sonnet-5",
    }
    (tr_dir / "agent-priced.meta.json").write_text(json.dumps(meta_priced))

    meta_unpriced = {
        "agentType": "als-evidence",
        "description": "Unpriced Agent",
        "workflowPhase": "evidence",
        "model": "unknown-model",
    }
    (tr_dir / "agent-unpriced.meta.json").write_text(json.dumps(meta_unpriced))

    # Unpriced first, then priced (test that order doesn't matter)
    transcript_lines = [
        {
            "requestId": "unpriced-req-001",
            "timestamp": "2026-09-26T12:00:00.000Z",
            "message": {"usage": {"input_tokens": 100, "output_tokens": 50}},
        },
        {
            "requestId": "priced-req-001",
            "timestamp": "2026-09-26T12:00:01.000Z",
            "message": {
                "usage": {
                    "input_tokens": 200,
                    "output_tokens": 100,
                    "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 0},
                    "cache_read_input_tokens": 0,
                }
            },
        },
    ]

    (tr_dir / "agent-priced.jsonl").write_text("\n".join(json.dumps(line) for line in [transcript_lines[1]]))
    (tr_dir / "agent-unpriced.jsonl").write_text("\n".join(json.dumps(line) for line in [transcript_lines[0]]))

    records = read_transcripts(tr_dir)
    limits = {}
    prices = {
        "models": {"claude-sonnet-5": {"input": 1, "output": 5}},
        "multipliers": {},
    }
    result = aggregate(records, limits, prices)

    # est_usd should be numeric (from priced part only)
    assert isinstance(result["totals"]["est_usd"], float)
    assert result["totals"]["est_usd"] > 0
    # unpriced_models should list unknown-model
    assert "unknown-model" in result["unpriced_models"]
    # unpriced_tokens should include unpriced record's input + output
    assert result["unpriced_tokens"] == 100 + 50
    # priced should be False for unpriced agent
    assert result["by_agent"]["unpriced"]["priced"] is False
    assert result["by_agent"]["priced"]["priced"] is True


def test_aggregate_zero_cost() -> None:
    """Test zero cost gives 0.0, not None."""
    records = [
        UsageRecord(
            agent_id="test",
            label="test",
            agent_type="test",
            phase="test",
            model="claude-sonnet-5",
            request_id="req-1",
            timestamp="2026-09-26T12:00:00Z",
            input_tokens=0,
            cache_creation_5m=0,
            cache_creation_1h=0,
            cache_read=0,
            output_tokens=0,
        )
    ]
    limits = {}
    prices = {
        "models": {"claude-sonnet-5": {"input": 1, "output": 5}},
        "multipliers": {},
    }
    result = aggregate(records, limits, prices)

    assert result["totals"]["est_usd"] == 0.0
    assert result["totals"]["est_usd"] is not None


def test_aggregate_raw_vs_billed_equivalent() -> None:
    """Test raw vs billed-equivalent values for cache operations."""
    records = [
        UsageRecord(
            agent_id="test",
            label="test",
            agent_type="test",
            phase="test",
            model="claude-sonnet-5",
            request_id="req-1",
            timestamp="2026-09-26T12:00:00Z",
            input_tokens=100,
            cache_creation_5m=10,  # 10 raw, 10*1.25=12.5 billed
            cache_creation_1h=20,  # 20 raw, 20*2.0=40 billed
            cache_read=50,  # 50 raw, 50*0.1=5 billed
            output_tokens=200,
        )
    ]
    limits = {}
    prices = {
        "models": {"claude-sonnet-5": {"input": 1, "output": 1}},
        "multipliers": {"cache_write_5m": 1.25, "cache_write_1h": 2.0, "cache_read": 0.1},
    }
    result = aggregate(records, limits, prices)

    # Raw totals
    assert result["totals"]["input"]["fresh"] == 100
    assert result["totals"]["input"]["cache_write"] == 10 + 20  # raw
    assert result["totals"]["input"]["cache_read"] == 50  # raw
    assert result["totals"]["input"]["total"] == 100 + 30 + 50

    # Billed equivalent
    expected_billed = 100 + 12.5 + 40 + 5
    assert result["totals"]["input_billed_equivalent"] == expected_billed


def test_aggregate_limit_exceeded_on_raw_tokens() -> None:
    """Test that limits are enforced on raw tokens, not billed equivalents."""
    records = [
        UsageRecord(
            agent_id="test",
            label="test",
            agent_type="test",
            phase="test",
            model="claude-sonnet-5",
            request_id="req-1",
            timestamp="2026-09-26T12:00:00Z",
            input_tokens=1000,
            cache_creation_5m=0,
            cache_creation_1h=0,
            cache_read=100,  # raw=100, billed=10
            output_tokens=0,
        )
    ]
    limits = {"max_input_tokens": 500}  # Limit on raw tokens
    prices = {
        "models": {"claude-sonnet-5": {"input": 1, "output": 1}},
        "multipliers": {"cache_read": 0.1},
    }
    result = aggregate(records, limits, prices)

    # Raw total is 1100, should exceed limit of 500
    assert "max_input_tokens" in result["limits"]["exceeded"]
    assert result["status"] == "cap_exceeded"


def test_aggregate_status_passthrough_and_cap_exceeded_override() -> None:
    """Test that caller's status is used, but overridden to cap_exceeded."""
    records = [
        UsageRecord(
            agent_id="test",
            label="test",
            agent_type="test",
            phase="test",
            model="claude-sonnet-5",
            request_id="req-1",
            timestamp="2026-09-26T12:00:00Z",
            input_tokens=100,
            cache_creation_5m=0,
            cache_creation_1h=0,
            cache_read=0,
            output_tokens=0,
        )
    ]
    limits = {}
    prices = {
        "models": {"claude-sonnet-5": {"input": 1, "output": 1}},
        "multipliers": {},
    }

    # Test status passthrough when no limits exceeded
    result = aggregate(records, limits, prices, status="completed", source="saved")
    assert result["status"] == "completed"
    assert result["source"] == "saved"

    # Test cap_exceeded overrides caller's status
    result = aggregate(records, {"max_input_tokens": 50}, prices, status="running", source="live")  # Will be exceeded
    assert result["status"] == "cap_exceeded"
    assert result["source"] == "live"


def test_collect_records_deduplicates_same_transcript_dir(tmp_path: Path) -> None:
    """Two streams reporting the same workflow launch count its agents once."""
    tr_dir = _workflow_dir(tmp_path)
    (tr_dir / "agent-test.jsonl").write_text(
        json.dumps(
            {
                "requestId": "req-001",
                "sessionId": SESSION_ID,
                "agentId": "test",
                "message": {"usage": {"input_tokens": 100, "output_tokens": 50}},
            }
        )
    )
    stream1, stream2 = tmp_path / "stream1.jsonl", tmp_path / "stream2.jsonl"
    stream1.write_text(json.dumps(_launch_line(tr_dir)))
    stream2.write_text(json.dumps(_launch_line(tr_dir)))

    records, workflows = collect_usage([stream1, stream2])

    assert [(r.agent_id, r.input_tokens, r.workflow_run_id) for r in records] == [("test", 100, RUN_ID)]
    assert [w["run_id"] for w in workflows] == [RUN_ID]


def test_aggregate_schema_version_and_series_raw() -> None:
    """Test that schema_version is 3 and series uses raw tokens."""
    records = [
        UsageRecord(
            agent_id="test",
            label="test",
            agent_type="test",
            phase="test",
            model="claude-sonnet-5",
            request_id="req-1",
            timestamp="2026-09-26T12:00:00Z",
            input_tokens=100,
            cache_creation_5m=10,
            cache_creation_1h=0,
            cache_read=5,
            output_tokens=50,
        )
    ]
    limits = {}
    prices = {
        "models": {"claude-sonnet-5": {"input": 1, "output": 1}},
        "multipliers": {"cache_write_5m": 1.25, "cache_read": 0.1},
    }
    result = aggregate(records, limits, prices)

    assert result["schema_version"] == 3
    # Series input is raw (100+10+5=115)
    assert result["series"][0]["input"] == 115
    assert result["series"][0]["cumulative_input"] == 115


def _write_transcript(tmp_path: Path, lines: list[dict]) -> Path:
    tdir = tmp_path / "transcripts"
    tdir.mkdir()
    (tdir / "agent-a1.meta.json").write_text(json.dumps({"agentType": "als-critic", "model": "claude-sonnet-5"}))
    (tdir / "agent-a1.jsonl").write_text("\n".join(json.dumps(line) for line in lines) + "\n")
    return tdir


def test_read_transcripts_keeps_final_line_of_streamed_request(tmp_path: Path) -> None:
    """A streamed response is logged as several lines per requestId; only the last has the final output count."""
    usage = {"input_tokens": 8, "cache_read_input_tokens": 100}
    lines = [
        {"requestId": "req_1", "message": {"usage": {**usage, "output_tokens": 1}}},
        {"requestId": "req_1", "message": {"usage": {**usage, "output_tokens": 1}}},
        {"requestId": "req_1", "message": {"stop_reason": "tool_use", "usage": {**usage, "output_tokens": 348}}},
    ]
    records = read_transcripts(_write_transcript(tmp_path, lines))
    assert len(records) == 1
    assert records[0].output_tokens == 348
    assert records[0].input_tokens == 8


def test_read_transcripts_cache_write_total_without_split_counts_as_5m(tmp_path: Path) -> None:
    lines = [
        {
            "requestId": "req_1",
            "message": {"usage": {"input_tokens": 2, "cache_creation_input_tokens": 11482, "output_tokens": 8}},
        }
    ]
    [record] = read_transcripts(_write_transcript(tmp_path, lines))
    assert (record.cache_creation_5m, record.cache_creation_1h) == (11482, 0)


def test_read_transcripts_cache_write_split_is_read(tmp_path: Path) -> None:
    usage = {
        "input_tokens": 2,
        "cache_creation_input_tokens": 30,
        "cache_creation": {"ephemeral_5m_input_tokens": 10, "ephemeral_1h_input_tokens": 20},
        "output_tokens": 1,
    }
    [record] = read_transcripts(_write_transcript(tmp_path, [{"requestId": "r", "message": {"usage": usage}}]))
    assert (record.cache_creation_5m, record.cache_creation_1h) == (10, 20)


def test_read_transcripts_null_cache_fields_count_as_zero(tmp_path: Path) -> None:
    """The SDK types the cache fields as Optional[int]; null must not crash the sums."""
    usage = {
        "input_tokens": 3,
        "cache_creation_input_tokens": None,
        "cache_read_input_tokens": None,
        "cache_creation": None,
        "output_tokens": 4,
        "speed": "standard",
        "iterations": [],
    }
    [record] = read_transcripts(_write_transcript(tmp_path, [{"requestId": "r", "message": {"usage": usage}}]))
    assert (record.input_tokens, record.cache_creation_5m, record.cache_read, record.output_tokens) == (3, 0, 0, 4)


@pytest.mark.parametrize("bad", [-1, "12", 1.5, True])
def test_read_transcripts_rejects_malformed_token_counts(tmp_path: Path, bad: object) -> None:
    usage = {"input_tokens": bad, "output_tokens": 1}
    with pytest.raises(ValueError, match=r"usage\.input_tokens.*agent-a1\.jsonl:1"):
        read_transcripts(_write_transcript(tmp_path, [{"requestId": "r", "message": {"usage": usage}}]))


def test_read_orchestrator_usage_without_message_id_raises(tmp_path: Path) -> None:
    stream = tmp_path / "stream.jsonl"
    stream.write_text(json.dumps({"type": "assistant", "message": {"usage": {"input_tokens": 1, "output_tokens": 1}}}))
    with pytest.raises(ValueError, match="Missing message.id"):
        read_orchestrator(stream)


def test_collect_records_keeps_most_complete_record_across_streams(tmp_path: Path) -> None:
    """The same message id in two streams keeps the final count, whatever the stream order."""

    def stream(name: str, output: int) -> Path:
        path = tmp_path / name
        path.write_text(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {"id": "msg_1", "model": "m", "usage": {"input_tokens": 1, "output_tokens": output}},
                }
            )
        )
        return path

    final, partial = stream("a.jsonl", 90), stream("b.jsonl", 1)
    assert [r.output_tokens for r in collect_records([final, partial])] == [90]
    assert [r.output_tokens for r in collect_records([partial, final])] == [90]


def test_aggregate_is_independent_of_record_order() -> None:
    def rec(agent: str, req: str) -> UsageRecord:
        return UsageRecord(agent, agent, "t", "p", "m", req, "2026-09-26T12:00:00.000Z", 1, 0, 0, 0, 1)

    records = [rec("b", "r2"), rec("a", "r1"), rec("c", "r3")]
    first = aggregate(records, {}, {"models": {}, "multipliers": {}})
    second = aggregate(list(reversed(records)), {}, {"models": {}, "multipliers": {}})
    for result in (first, second):
        result.pop("generated_at")
    assert json.dumps(first) == json.dumps(second)


def test_write_snapshot_leaves_no_temp_file_on_failure(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        write_snapshot(tmp_path, {"totals": float("nan")})
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "prices",
    [
        {"models": {}},
        {"models": {}, "multipliers": {"cache_read": 0.1, "cache_write_5m": 1.25}},
        {
            "models": {"m": {"input": "4"}},
            "multipliers": {"cache_read": 0.1, "cache_write_5m": 1.25, "cache_write_1h": 2},
        },
    ],
)
def test_validate_tokens_config_rejects_incomplete_price_table(prices: dict) -> None:
    with pytest.raises(ValueError, match="tokens config"):
        validate_tokens_config({"prices_usd_per_mtok": prices})


def test_repository_tokens_config_is_valid() -> None:
    assert load_tokens_config()["prices_usd_per_mtok"]["multipliers"]


def _record(
    agent: str,
    req: str,
    ts: str,
    *,
    agent_type: str = "t",
    model: str = "m",
    speed: str = "standard",
    inp: int = 1,
    out: int = 1,
) -> UsageRecord:
    return UsageRecord(agent, agent, agent_type, "p", model, req, ts, inp, 0, 0, 0, out, speed)


def test_series_per_request_with_per_role_running_totals() -> None:
    records = [
        _record("a", "r1", "2026-09-26T12:00:00Z", agent_type="x", inp=10, out=1),
        _record("b", "r2", "2026-09-26T12:00:01Z", agent_type="y", inp=20, out=2),
        _record("a", "r3", "2026-09-26T12:00:02Z", agent_type="x", inp=30, out=3),
    ]
    series = aggregate(records, {}, {"models": {}, "multipliers": {}})["series"]
    assert [
        (p["role"], p["request_id"], p["input"], p["output"], p["cumulative_input"], p["cumulative_output"])
        for p in series
    ] == [("x", "r1", 10, 1, 10, 1), ("y", "r2", 20, 2, 20, 2), ("x", "r3", 30, 3, 40, 4)]
    assert list(series[0]) == [
        "timestamp",
        "role",
        "agent_id",
        "request_id",
        "input",
        "output",
        "cumulative_input",
        "cumulative_output",
    ]


def test_fast_mode_requests_are_visibly_unpriced() -> None:
    prices = {
        "models": {"m": {"input": 4, "output": 20}},
        "multipliers": {"cache_read": 0.1, "cache_write_5m": 1.25, "cache_write_1h": 2.0},
    }
    records = [
        _record("a", "r1", "2026-09-26T12:00:00Z", inp=1_000_000, out=0),
        _record("b", "r2", "2026-09-26T12:00:01Z", speed="fast", inp=500, out=7),
    ]
    result = aggregate(records, {}, prices)
    assert result["totals"]["est_usd"] == 4.0  # the fast request adds nothing
    assert result["unpriced_models"] == ["m (speed=fast)"]
    assert result["unpriced_tokens"] == 507
    assert (result["by_agent"]["a"]["priced"], result["by_agent"]["b"]["priced"]) == (True, False)
    assert result["totals"]["input"]["total"] == 1_000_500  # still counted in raw totals and limits


def test_read_transcripts_reads_speed(tmp_path: Path) -> None:
    lines = [
        {"requestId": "r1", "message": {"usage": {"input_tokens": 1, "output_tokens": 1, "speed": "fast"}}},
        {"requestId": "r2", "message": {"usage": {"input_tokens": 1, "output_tokens": 1}}},
    ]
    assert [r.speed for r in read_transcripts(_write_transcript(tmp_path, lines))] == ["fast", "standard"]


def test_read_transcripts_rejects_malformed_speed(tmp_path: Path) -> None:
    lines = [{"requestId": "r1", "message": {"usage": {"input_tokens": 1, "output_tokens": 1, "speed": 2}}}]
    with pytest.raises(ValueError, match="usage.speed"):
        read_transcripts(_write_transcript(tmp_path, lines))


def test_aggregate_reports_applied_pricing_basis() -> None:
    """The snapshot records the multipliers it applied, which the UI shows in the USD tooltip."""
    prices = {"source": "test table", "models": {}, "multipliers": {"cache_read": 0.2}}
    for records in ([], [_record("a", "r1", "2026-09-26T12:00:00Z")]):
        assert aggregate(records, {}, prices)["pricing"] == {
            "source": "test table",
            "priced_speed": "standard",
            "multipliers": {"cache_read": 0.2, "cache_write_5m": 1.25, "cache_write_1h": 2.0},
        }


def test_the_tokens_view_labels_the_estimate_and_names_both_cost_figures() -> None:
    """The reconstructed estimate and the CLI's own figure can disagree, so neither may be shown
    unqualified as "cost"."""
    js_root = Path(__file__).parents[1] / "observability/wf_observer/static/js"
    tokens_view = (js_root / "run" / "tokens.js").read_text()
    assert "usdLabel(" in tokens_view
    assert "cost_usd" in tokens_view  # the figure the CLI reported, shown alongside
    assert "Estimated cost" in (js_root / "dom.js").read_text()


def _agent(
    tr_dir: Path, agent_id: str, *, session_id: str = SESSION_ID, journal: bool = True, transcript: bool = True
) -> None:
    if journal:
        with open(tr_dir / "journal.jsonl", "a") as f:
            f.write(json.dumps({"type": "started", "agentId": agent_id, "label": f"L-{agent_id}", "phase": "P"}) + "\n")
    if transcript:
        (tr_dir / f"agent-{agent_id}.meta.json").write_text(json.dumps({"agentType": "t", "model": "m"}))
        (tr_dir / f"agent-{agent_id}.jsonl").write_text(
            json.dumps(
                {
                    "requestId": f"req-{agent_id}",
                    "sessionId": session_id,
                    "agentId": agent_id,
                    "message": {"usage": {"input_tokens": 1, "output_tokens": 1}},
                }
            )
        )


def test_collect_usage_links_every_agent_of_every_launch(tmp_path: Path) -> None:
    """A stream that relaunches a workflow keeps both runs; each agent is tagged with its own runId."""
    first, second = _workflow_dir(tmp_path, run_id="wf_a"), _workflow_dir(tmp_path, run_id="wf_b")
    _agent(first, "a1")
    _agent(second, "b1")
    _agent(second, "b2")
    stream = tmp_path / "stream.jsonl"
    stream.write_text("\n".join(json.dumps(_launch_line(d, run_id=d.name)) for d in (first, second)))

    records, workflows = collect_usage([stream])

    assert sorted((r.agent_id, r.workflow_run_id) for r in records) == [("a1", "wf_a"), ("b1", "wf_b"), ("b2", "wf_b")]
    assert [(w["run_id"], w["state"], [a["agent_id"] for a in w["agents"]]) for w in workflows] == [
        ("wf_a", "ok", ["a1"]),
        ("wf_b", "ok", ["b1", "b2"]),
    ]
    by_agent = aggregate(records, {}, {"models": {}, "multipliers": {}})["by_agent"]
    assert by_agent["b2"]["workflow_run_id"] == "wf_b"


def test_collect_usage_workflow_states(tmp_path: Path) -> None:
    empty = _workflow_dir(tmp_path, run_id="wf_empty")
    (empty / "journal.jsonl").write_text('{"type":"launched"}\n')
    partial = _workflow_dir(tmp_path, run_id="wf_partial")
    _agent(partial, "p1")
    _agent(partial, "p2", transcript=False)
    gone = tmp_path / SESSION_ID / "subagents" / "workflows" / "wf_gone"
    streams = []
    for name, tr_dir in [("empty", empty), ("partial", partial), ("gone", gone)]:
        streams.append(tmp_path / f"{name}.jsonl")
        streams[-1].write_text(json.dumps(_launch_line(tr_dir, run_id=tr_dir.name)))
    streams.append(tmp_path / "none.jsonl")
    streams[-1].write_text(json.dumps({"type": "system", "subtype": "init", "session_id": SESSION_ID}))

    _, workflows = collect_usage(streams + [tmp_path / "absent.jsonl"])

    assert [(w["stream"], w["state"]) for w in workflows] == [
        ("empty.jsonl", "no_agents"),
        ("partial.jsonl", "incomplete"),
        ("gone.jsonl", "missing"),
        ("none.jsonl", "no_launch"),
    ]
    assert [(a["agent_id"], a["in_journal"], a["transcript"]) for a in workflows[1]["agents"]] == [
        ("p1", True, True),
        ("p2", True, False),
    ]


def test_transcript_of_another_session_is_rejected(tmp_path: Path) -> None:
    tr_dir = _workflow_dir(tmp_path)
    _agent(tr_dir, "x1", session_id="other-session")
    stream = tmp_path / "stream.jsonl"
    stream.write_text(json.dumps(_launch_line(tr_dir)))
    with pytest.raises(ValueError, match="not linked to workflow"):
        collect_usage([stream])


def test_launch_with_unexpected_transcript_dir_is_rejected(tmp_path: Path) -> None:
    stray = tmp_path / "somewhere"
    stray.mkdir()
    stream = tmp_path / "stream.jsonl"
    stream.write_text(json.dumps(_launch_line(stray)))
    with pytest.raises(ValueError, match="subagents/workflows"):
        workflow_launches(stream)


def test_text_only_transcript_pointer_is_not_trusted(tmp_path: Path) -> None:
    tr_dir = _workflow_dir(tmp_path)
    _agent(tr_dir, "t1")
    stream = tmp_path / "stream.jsonl"
    stream.write_text(
        json.dumps(
            {"type": "user", "message": {"content": [{"type": "tool_result", "content": f"Transcript dir: {tr_dir}"}]}}
        )
    )
    assert workflow_launches(stream) == []


def test_server_live_tokens_report_workflows(tmp_path: Path) -> None:
    smoke_root = tmp_path / "smoke"
    run = smoke_root / "20260926T120000Z"
    run.mkdir(parents=True)
    tr_dir = _workflow_dir(tmp_path)
    _agent(tr_dir, "s1")
    (run / "stream.jsonl").write_text(json.dumps(_launch_line(tr_dir)))
    (tmp_path / "e2e").mkdir()
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), make_handler(RunStore(tmp_path / "runs"), smoke_root=smoke_root, e2e_root=tmp_path / "e2e")
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/tokens/20260926T120000Z?mode=live"
        with urllib.request.urlopen(url) as r:
            data = json.load(r)
        assert data["by_agent"]["s1"]["workflow_run_id"] == RUN_ID
        [workflow] = data["workflows"]
        assert (workflow["run_id"], workflow["state"], workflow["agents"][0]["agent_id"]) == (RUN_ID, "ok", "s1")
    finally:
        server.shutdown()
        server.server_close()
