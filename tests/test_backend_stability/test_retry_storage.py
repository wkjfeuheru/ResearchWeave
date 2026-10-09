"""Retry guarantees, complete-event bounds, schema migration and bounded retention."""

import asyncio
import json
import multiprocessing
import sqlite3

import pytest

from researchx.api.client import ApiMessageCompleteEvent, ApiMessageRequest, ApiTextDeltaEvent
from researchx.api.retry import classify_error, stream_with_retry
from researchx.api.usage import UsageSnapshot
from researchx.engine.messages import ConversationMessage, ToolUseBlock
from researchx.services.execution.operations import OperationStore
from researchx.services.execution.tool_execution import ToolExecutionService
from researchx.tools.base import ToolResult
from researchx.tools.contracts import resolve_contract
from tests.test_harness.test_execution import setup, Write
from tests.test_backend_stability.storage_worker import open_store


@pytest.mark.parametrize("mode", ["never", "idempotent", "reconcile_before_retry"])
@pytest.mark.parametrize("retryable", [False, True])
async def test_domain_errors_do_not_retry_and_transients_require_mode(
    tmp_path, monkeypatch, mode, retryable
):
    class Retry(Write):
        contract = {
            "name": Write.name,
            "effect": "read_only",
            "retry_mode": mode,
            "max_attempts": 1 if mode == "never" else 2,
        }

        async def execute(self, arguments, context):
            self.calls += 1
            return ToolResult("failure", is_error=True, retryable=retryable)

        async def reconcile_no_effect(self, arguments, context):
            return True

    tool, context = setup(tmp_path, monkeypatch, Retry())
    result = await ToolExecutionService().execute(context, tool.name, "retry", {"value": 1})
    assert result.is_error and tool.calls == (2 if retryable and mode != "never" else 1)


@pytest.mark.parametrize(
    "code",
    ["authentication", "authorization", "invalid_request", "quota_exhausted", "context_length"],
)
async def test_declared_nontransient_error_never_retries(tmp_path, monkeypatch, code):
    class Retry(Write):
        contract = {
            "name": Write.name,
            "effect": "read_only",
            "retry_mode": "idempotent",
            "max_attempts": 3,
        }

        async def execute(self, arguments, context):
            self.calls += 1
            return ToolResult("do not retry", is_error=True, retryable=True, error_code=code)

    tool, context = setup(tmp_path, monkeypatch, Retry())
    await ToolExecutionService().execute(context, tool.name, "nontransient", {"value": 1})
    assert tool.calls == 1


def test_key_retry_requires_remote_guarantee_and_actual_adapter():
    class Missing(Write):
        contract = {
            "name": Write.name,
            "effect": "external_write",
            "retry_mode": "idempotency_key",
            "max_attempts": 2,
        }

    with pytest.raises(ValueError, match="remote guarantee"):
        resolve_contract(Missing())
    Missing.contract["idempotency_key_supported"] = True
    with pytest.raises(ValueError, match="adapter"):
        resolve_contract(Missing())


async def test_key_adapter_receives_and_uses_one_key_for_the_remote_operation(
    tmp_path, monkeypatch
):
    remote = {}
    keys = []

    class KeyAdapter(Write):
        contract = {
            "name": Write.name,
            "effect": "external_write",
            "retry_mode": "idempotency_key",
            "max_attempts": 2,
            "idempotency_key_supported": True,
        }

        async def execute(self, arguments, context):
            raise AssertionError("Generic execution must not bypass the key adapter")

        async def execute_with_idempotency_key(self, arguments, context, *, idempotency_key):
            keys.append(idempotency_key)
            if len(keys) == 1:
                return ToolResult(
                    "request rejected before write", is_error=True, retryable=True, no_effect=True
                )
            remote.setdefault(idempotency_key, arguments.value)
            return ToolResult("remote committed")

    tool, context = setup(tmp_path, monkeypatch, KeyAdapter())
    result = await ToolExecutionService().execute(context, tool.name, "key-op", {"value": 8})
    assert not result.is_error and len(keys) == 2 and len(set(keys)) == 1
    assert remote == {keys[0]: 8}
    replay = await ToolExecutionService().execute(context, tool.name, "key-op", {"value": 8})
    assert not replay.is_error and len(keys) == 2


async def test_default_reconciler_false_does_not_prove_absence(tmp_path, monkeypatch):
    class Unverified(Write):
        contract = {
            "name": Write.name,
            "effect": "external_write",
            "retry_mode": "reconcile_before_retry",
            "max_attempts": 2,
        }

        async def execute(self, arguments, context):
            self.calls += 1
            return ToolResult("unknown result", is_error=True, retryable=True)

    tool, context = setup(tmp_path, monkeypatch, Unverified())
    result = await ToolExecutionService().execute(context, tool.name, "uncertain", {"value": 1})
    assert tool.calls == 1 and result.result_metadata["status"] == "uncertain"


@pytest.mark.parametrize(
    "code,expected",
    [
        ("insufficient_quota", "quota_exhausted"),
        ("rate_limit_exceeded", "rate_limit"),
        ("context_length_exceeded", "context_length"),
        ("invalid_request_error", "invalid_request"),
    ],
)
def test_provider_structured_code_wins_over_generic_status(code, expected):
    class ProviderError(Exception):
        status_code = 429
        body = {"error": {"code": code}}

    assert classify_error(ProviderError("generic provider message")) == expected


async def test_full_message_event_limit_discards_partial_and_closes_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCHX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr("researchx.api.retry.MAX_BUFFER_BYTES", 1000)
    records, delivered = [], []
    closed = False

    async def stream(request):
        nonlocal closed
        try:
            yield ApiTextDeltaEvent("partial must not escape")
            yield ApiMessageCompleteEvent(
                ConversationMessage(
                    role="assistant",
                    content=[ToolUseBlock(name="large_tool", input={"payload": "X" * 2000})],
                ),
                UsageSnapshot(),
            )
        finally:
            closed = True

    request = ApiMessageRequest(model="test", messages=[], attempt_callback=records.append)
    with pytest.raises(ValueError, match="event limit"):
        async for event in stream_with_retry(stream, request, translate=lambda exc: exc):
            delivered.append(event)
    assert closed and not delivered and len(records) == 1
    assert records[0]["usage_status"] == "unknown" and records[0]["usage"] is None


async def test_cancel_still_propagates_when_settlement_audit_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCHX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(
        "researchx.api.retry._record",
        lambda *args: (_ for _ in ()).throw(sqlite3.OperationalError("locked")),
    )
    entered = asyncio.Event()

    async def stream(request):
        entered.set()
        await asyncio.sleep(30)
        yield ApiTextDeltaEvent("never")

    async def run():
        return [
            e
            async for e in stream_with_retry(
                stream, ApiMessageRequest(model="test", messages=[]), translate=lambda exc: exc
            )
        ]

    task = asyncio.create_task(run())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with OperationStore(tmp_path / "data/executions/operations.sqlite3").connect() as db:
        record = json.loads(db.execute("SELECT record FROM api_attempts").fetchone()[0])
        assert record["status"] == "running" and record["usage_status"] == "unknown"


def test_schema_initialized_once_with_hot_query_indexes(tmp_path, monkeypatch):
    statements = []
    real = sqlite3.connect

    def traced(*args, **kwargs):
        db = real(*args, **kwargs)
        db.set_trace_callback(statements.append)
        return db

    monkeypatch.setattr("researchx.services.execution.operations.sqlite3.connect", traced)
    path = tmp_path / "private/operations.sqlite3"
    first = OperationStore(path)
    statements.clear()
    OperationStore(path)
    assert not any("CREATE " in s or "journal_mode" in s or "user_version" in s for s in statements)
    with first.connect() as db:
        plan = db.execute(
            "EXPLAIN QUERY PLAN SELECT resources FROM operations WHERE scope=? AND status IN ('running','uncertain','partial')",
            ("w",),
        ).fetchall()
        assert any("operation_scope_status" in row[3] for row in plan)
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2


def test_first_initialization_is_safe_across_processes(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    path = tmp_path / "private/operations.sqlite3"
    workers = [ctx.Process(target=open_store, args=(path, queue)) for _ in range(3)]
    try:
        for worker in workers:
            worker.start()
        results = [queue.get(timeout=20) for _ in workers]
        for worker in workers:
            worker.join(timeout=20)
        assert results == ["ok"] * 3
        assert all(worker.exitcode == 0 for worker in workers)
        with OperationStore(path).connect() as db:
            assert db.execute("SELECT count(*) FROM operations").fetchone()[0] == 3
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(5)
        queue.close()


def test_legacy_api_audit_migration_preserves_records(tmp_path):
    path = tmp_path / "legacy.db"
    record = {"status": "failed", "usage_status": "unknown", "usage": None}
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE api_attempts(attempt_id TEXT PRIMARY KEY,request_id TEXT NOT NULL,record TEXT NOT NULL)"
        )
        db.execute("INSERT INTO api_attempts VALUES(?,?,?)", ("legacy", "req", json.dumps(record)))
    store = OperationStore(path)
    with store.connect() as db:
        assert json.loads(db.execute("SELECT record FROM api_attempts").fetchone()[0]) == record
    assert store.prune_audit(before=2, limit=10) == {"api_attempts": 0, "tool_attempts": 0}


def test_audit_retention_is_bounded_and_keeps_unresolved_receipts(tmp_path):
    store = OperationStore(tmp_path / "private/operations.sqlite3")
    for status in (
        "succeeded",
        "failed",
        "cancelled",
        "partial",
        "uncertain",
        "running",
        "prepared",
        "blocked",
    ):
        row = store.prepare(
            session="s",
            scope=status,
            run=status,
            call=status,
            tool="tool",
            version="1",
            digest="digest",
            effect="external_write",
            resources={"read": [], "write": [status]},
        )
        op = row["operation_id"]
        if status != "prepared":
            assert store.claim(op, "owner")
            if status != "running":
                store.settle(op, status, owner="owner")
    for i in range(3):
        store.record_api_attempt(
            {
                "attempt_id": f"api-{i}",
                "request_id": "request",
                "status": "succeeded",
                "usage_status": "reported",
                "finished": 1,
            }
        )
    store.record_api_attempt(
        {
            "attempt_id": "unknown",
            "request_id": "request",
            "status": "failed",
            "usage_status": "unknown",
            "finished": 1,
        }
    )
    with store.transaction() as db:
        db.execute("UPDATE operations SET updated=1")
        db.execute("UPDATE attempts SET updated=1")
    assert store.prune_audit(before=2, limit=2) == {"api_attempts": 2, "tool_attempts": 0}
    assert store.prune_audit(before=2, limit=100) == {"api_attempts": 1, "tool_attempts": 3}
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM operations").fetchone()[0] == 8
        assert {row[0] for row in db.execute("SELECT status FROM attempts")} == {
            "running",
            "partial",
            "uncertain",
            "blocked",
        }
        assert db.execute("SELECT attempt_id FROM api_attempts").fetchone()[0] == "unknown"
