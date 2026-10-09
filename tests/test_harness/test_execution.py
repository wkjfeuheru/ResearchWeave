"""Execution boundaries tested with real SQLite, hooks and side-effect files."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel

from researchx.config import Settings
from researchx.config.settings import PermissionSettings
from researchx.engine.query import QueryContext, _execute_tool_call_impl
from researchx.hooks import HookEvent, HookExecutor, HookExecutionContext
from researchx.hooks.loader import HookRegistry
from researchx.hooks.schemas import CommandHookDefinition
from researchx.permissions.checker import PermissionChecker
from researchx.permissions.capabilities import CapabilityContext
from researchx.permissions.modes import PermissionMode
from researchx.services.execution.operations import OperationStore
from researchx.tools.base import BaseTool, ToolRegistry, ToolResult
from researchx.tools.contracts import resolve_contract, ToolContract
from researchx.tools.file_write_tool import FileWriteTool


class Input(BaseModel):
    value: int


class Write(BaseTool):
    name = "write_probe"
    description = "Test side effect"
    input_model = Input
    contract = {"name": name, "effect": "external_write", "timeout_seconds": 0.05}

    def __init__(self):
        self.calls = 0
        self.wait = False
        self.entered = asyncio.Event()

    def is_read_only(self, arguments):
        return False

    async def execute(self, arguments, context):
        self.calls += 1
        (context.cwd / "side_effect").write_text(str(arguments.value))
        self.entered.set()
        if self.wait:
            await asyncio.sleep(30)
        return ToolResult("committed")


def setup(tmp_path, monkeypatch, tool=None, mode=PermissionMode.FULL_AUTO, hooks=None):
    monkeypatch.setattr(
        "researchx.services.execution.tool_execution.get_data_dir", lambda: tmp_path / "data"
    )
    tool = tool or Write()
    registry = ToolRegistry()
    registry.register(tool)
    context = QueryContext(
        api_client=AsyncMock(),
        tool_registry=registry,
        permission_checker=PermissionChecker(PermissionSettings(mode=mode)),
        cwd=tmp_path,
        model="test",
        system_prompt="test",
        max_tokens=100,
        execution_session_id="session",
        hook_executor=hooks,
    )
    return tool, context


def ledger(tmp_path):
    return OperationStore(tmp_path / "data/executions/operations.sqlite3")


@pytest.mark.asyncio
async def test_side_effect_receipt_restart_reuses_success_and_post_hook_failure(
    tmp_path, monkeypatch
):
    hooks = HookRegistry()
    hooks.register(
        HookEvent.POST_TOOL_USE, CommandHookDefinition(command="exit 7", block_on_failure=True)
    )
    executor = HookExecutor(
        hooks, HookExecutionContext(tmp_path, AsyncMock(), "test", settings=Settings())
    )
    tool, context = setup(tmp_path, monkeypatch, hooks=executor)
    first = await _execute_tool_call_impl(context, tool.name, "call", {"value": 42})
    assert not first.is_error
    assert first.result_metadata["post_hook_failures"] == ["command"]
    assert (tmp_path / "side_effect").read_text() == "42"
    # A new runtime/store, same durable identity; no reliance on chat JSON.
    restarted_tool, restarted = setup(tmp_path, monkeypatch)
    replay = await _execute_tool_call_impl(restarted, tool.name, "call", {"value": 42})
    assert not replay.is_error and replay.result_metadata["replayed_receipt"]
    assert restarted_tool.calls == 0 and tool.calls == 1
    with ledger(tmp_path).connect() as db:
        assert db.execute("SELECT status FROM operations").fetchone()[0] == "succeeded"
        assert db.execute("SELECT count(*) FROM attempts").fetchone()[0] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,arguments",
    [(PermissionMode.PLAN, {"value": 1}), (PermissionMode.FULL_AUTO, {"value": "bad"})],
)
async def test_denied_or_invalid_never_runs_pre_effect(tmp_path, monkeypatch, mode, arguments):
    hooks = HookRegistry()
    hooks.register(HookEvent.PRE_TOOL_USE, CommandHookDefinition(command="touch hook_effect"))
    executor = HookExecutor(
        hooks, HookExecutionContext(tmp_path, AsyncMock(), "test", settings=Settings())
    )
    tool, context = setup(tmp_path, monkeypatch, mode=mode, hooks=executor)
    result = await _execute_tool_call_impl(context, tool.name, "call", arguments)
    assert result.is_error and tool.calls == 0
    assert not (tmp_path / "hook_effect").exists()


@pytest.mark.asyncio
async def test_write_timeout_is_uncertain_and_new_call_cannot_repeat(tmp_path, monkeypatch):
    tool, context = setup(tmp_path, monkeypatch)
    tool.wait = True
    first = await _execute_tool_call_impl(context, tool.name, "call", {"value": 42})
    assert first.result_metadata["status"] == "uncertain"
    tool.wait = False
    second = await _execute_tool_call_impl(context, tool.name, "different-call", {"value": 42})
    assert second.result_metadata["error_code"] == "reconciliation_required"
    assert tool.calls == 1


@pytest.mark.asyncio
async def test_cancelled_write_retains_uncertain_receipt(tmp_path, monkeypatch):
    tool, context = setup(tmp_path, monkeypatch)
    tool.wait = True
    pending = asyncio.create_task(_execute_tool_call_impl(context, tool.name, "call", {"value": 1}))
    await tool.entered.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    with ledger(tmp_path).connect() as db:
        assert db.execute("SELECT status,error_code FROM operations").fetchone()[:] == (
            "uncertain",
            "cancelled_after_start",
        )


@pytest.mark.asyncio
async def test_two_executors_same_operation_only_execute_once(tmp_path, monkeypatch):
    class SlowWrite(Write):
        async def execute(self, arguments, context):
            await asyncio.sleep(0.01)
            return await super().execute(arguments, context)

    tool, context = setup(tmp_path, monkeypatch, SlowWrite())
    a, b = await asyncio.gather(
        *[_execute_tool_call_impl(context, tool.name, "call", {"value": 1}) for _ in range(2)]
    )
    assert not a.is_error and not b.is_error
    assert tool.calls == 1


@pytest.mark.asyncio
async def test_cancel_waiter_does_not_settle_other_owner(tmp_path, monkeypatch):
    tool, context = setup(tmp_path, monkeypatch)
    tool.wait = True
    first = asyncio.create_task(_execute_tool_call_impl(context, tool.name, "call", {"value": 1}))
    await tool.entered.wait()
    waiter = asyncio.create_task(_execute_tool_call_impl(context, tool.name, "call", {"value": 1}))
    await asyncio.sleep(0.005)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first


def prepared(store, **kwargs):
    return store.prepare(
        session="s",
        scope="workspace",
        run="r",
        call="c",
        tool="write",
        version="1",
        digest="hash-only",
        effect="external_write",
        resources={"read": [], "write": ["*"]},
        **kwargs,
    )


def test_crash_recovery_requires_reconciliation(tmp_path):
    store = OperationStore(tmp_path / "operations.db")
    row = prepared(store)
    op = row["operation_id"]
    assert store.claim(op, "dead-owner")
    with store.transaction() as db:
        db.execute("UPDATE operations SET pid=?", (2147483647,))
    restart = OperationStore(store.path)
    recovered = restart.recover(session="s", scope="workspace")
    assert recovered[0]["status"] == "uncertain"
    assert restart.claim(op, "new-owner") is None
    with pytest.raises(ValueError, match="evidence"):
        restart.settle(op, "succeeded")
    restart.settle(op, "succeeded", evidence="external system confirmed request ID 123")
    with pytest.raises(ValueError, match="Illegal"):
        restart.settle(op, "running")
    with pytest.raises(ValueError, match="Unknown"):
        restart.get(op, session="other", scope="workspace")


def test_registry_conflicts_and_model_schema_compatibility():
    registry = ToolRegistry()
    registry.register(FileWriteTool())
    before = registry.to_api_schema()
    with pytest.raises(ValueError):
        registry.register(FileWriteTool())
    with pytest.raises(ValueError):
        registry.register(FileWriteTool(), replace=True)
    assert registry.to_api_schema() == before
    assert all("contract" not in item and "retry_mode" not in item for item in before)


def test_legacy_readonly_does_not_imply_retry_or_parallel():
    class Legacy(Write):
        contract = None

        def is_read_only(self, arguments):
            return True

    contract = resolve_contract(Legacy(), Input(value=1))
    assert contract.effect == "read_only"
    assert contract.retry_mode == "never" and contract.parallelism == "serial"
    with pytest.raises(ValueError):
        ToolContract(name="bad", retry_mode="never", max_attempts=2)


@pytest.mark.asyncio
async def test_metadata_cannot_expand_capabilities(tmp_path, monkeypatch):
    tool, context = setup(tmp_path, monkeypatch)
    tool.contract = {**tool.contract, "required_capabilities": ["network.http"]}
    context.capabilities = CapabilityContext(frozenset({"filesystem.read"}))
    context.tool_metadata = {"capabilities": ["*"]}
    result = await _execute_tool_call_impl(context, tool.name, "call", {"value": 1})
    assert result.result_metadata["error_code"] == "capability_denied"
    assert tool.calls == 0
    with pytest.raises(ValueError):
        context.capabilities.restrict(frozenset({"*"}))


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["idempotent", "idempotency_key", "reconcile_before_retry"])
async def test_contract_retries_only_verified_no_effect_and_preserves_key(
    tmp_path, monkeypatch, mode
):
    class RetryWrite(Write):
        contract = {
            "name": Write.name,
            "effect": "external_write",
            "retry_mode": mode,
            "max_attempts": 2,
            "idempotency_key_supported": mode == "idempotency_key",
        }
        keys = []
        reconciled = False

        async def execute(self, arguments, context):
            self.keys.append(context.idempotency_key)
            if len(self.keys) == 1:
                return ToolResult(
                    "not committed",
                    is_error=True,
                    metadata={"no_effect": mode != "reconcile_before_retry"},
                    retryable=True,
                )
            return await super().execute(arguments, context)

        async def execute_with_idempotency_key(self, arguments, context, *, idempotency_key):
            assert idempotency_key == context.idempotency_key
            return await self.execute(arguments, context)

        async def reconcile_no_effect(self, arguments, context):
            self.reconciled = True
            return True

    tool, context = setup(tmp_path, monkeypatch, RetryWrite())
    result = await _execute_tool_call_impl(context, tool.name, "retry", {"value": 7})
    assert not result.is_error and tool.calls == 1
    assert len(tool.keys) == 2 and len(set(tool.keys)) == 1
    assert tool.reconciled is (mode == "reconcile_before_retry")
    with ledger(tmp_path).connect() as db:
        assert db.execute("SELECT attempts,status FROM operations").fetchone()[:] == (
            2,
            "succeeded",
        )


@pytest.mark.asyncio
async def test_reconcile_unknown_does_not_repeat_external_write(tmp_path, monkeypatch):
    class Unsafe(Write):
        contract = {
            "name": Write.name,
            "effect": "external_write",
            "retry_mode": "reconcile_before_retry",
            "max_attempts": 2,
        }

        async def execute(self, arguments, context):
            self.calls += 1
            return ToolResult("connection lost after POST", is_error=True)

    tool, context = setup(tmp_path, monkeypatch, Unsafe())
    result = await _execute_tool_call_impl(context, tool.name, "unsafe", {"value": 7})
    assert result.result_metadata["status"] == "uncertain" and tool.calls == 1


@pytest.mark.asyncio
async def test_approval_cancel_does_not_start_tool(tmp_path, monkeypatch):
    tool, context = setup(tmp_path, monkeypatch, mode=PermissionMode.DEFAULT)
    entered = asyncio.Event()
    cleaned = asyncio.Event()

    async def prompt(*args):
        entered.set()
        try:
            await asyncio.sleep(30)
        finally:
            cleaned.set()

    context.permission_prompt = prompt
    pending = asyncio.create_task(
        _execute_tool_call_impl(context, tool.name, "approve", {"value": 1})
    )
    await entered.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert cleaned.is_set() and tool.calls == 0


@pytest.mark.asyncio
async def test_pre_hook_effect_failure_is_partial_not_safe_to_retry(tmp_path, monkeypatch):
    hooks = HookRegistry()
    hooks.register(
        HookEvent.PRE_TOOL_USE,
        CommandHookDefinition(command="touch hook_committed; exit 1", block_on_failure=True),
    )
    executor = HookExecutor(
        hooks, HookExecutionContext(tmp_path, AsyncMock(), "test", settings=Settings())
    )
    tool, context = setup(tmp_path, monkeypatch, hooks=executor)
    result = await _execute_tool_call_impl(context, tool.name, "call", {"value": 1})
    assert result.result_metadata["status"] == "partial"
    assert tool.calls == 0 and (tmp_path / "hook_committed").exists()
    with ledger(tmp_path).connect() as db:
        assert db.execute("SELECT status,effect FROM operations").fetchone()[:] == (
            "partial",
            "mixed",
        )


@pytest.mark.asyncio
async def test_missing_success_artifact_never_reexecutes(tmp_path, monkeypatch):
    from pathlib import Path

    tool, context = setup(tmp_path, monkeypatch)
    first = await _execute_tool_call_impl(context, tool.name, "call", {"value": 1})
    with ledger(tmp_path).connect() as db:
        saved = db.execute("SELECT result_ref FROM operations").fetchone()[0]
    Path(saved).unlink()
    result = await _execute_tool_call_impl(context, tool.name, "call", {"value": 1})
    assert not first.is_error and result.result_metadata["error_code"] == "artifact_unavailable"
    assert result.result_metadata["operation_status"] == "succeeded" and tool.calls == 1


@pytest.mark.asyncio
async def test_workspace_escape_rejected_before_pre_hook(tmp_path, monkeypatch):
    from researchx.state.models import ResearchObjective
    from researchx.state.runtime import ResearchAgentRuntime
    from researchx.state.store import ResearchStore
    from researchx.tools.file_read_tool import FileReadTool

    store = ResearchStore(tmp_path, "b" * 12, root=tmp_path / "state")
    store.capture(origin_id="user", kind="user", content="research")
    runtime = ResearchAgentRuntime(store, workspace_root=tmp_path / "workspaces")
    await runtime.start(
        ResearchObjective(
            project_id="p",
            subject="company",
            report_type="earnings",
            requirements=["sources"],
            deliverables=["note"],
        )
    )
    outside = tmp_path / "outside.txt"
    outside.write_text("private")
    registry = HookRegistry()
    registry.register(HookEvent.PRE_TOOL_USE, CommandHookDefinition(command="touch must_not_run"))
    hooks = HookExecutor(
        registry, HookExecutionContext(tmp_path, AsyncMock(), "test", settings=Settings())
    )
    hook_call = AsyncMock()
    monkeypatch.setattr(HookExecutor, "execute", hook_call)
    tool, context = setup(tmp_path, monkeypatch, FileReadTool(), hooks=hooks)
    context.tool_metadata = {"research_store": store, "research_runtime": runtime}
    result = await _execute_tool_call_impl(context, tool.name, "outside", {"path": str(outside)})
    assert result.is_error and result.result_metadata["error_code"] == "workspace_boundary"
    hook_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_recovery_cannot_reset_total_tool_attempt_limit(tmp_path, monkeypatch):
    from researchx.services.execution.tool_execution import ToolExecutionService

    class RetryRead(Write):
        contract = {
            "name": Write.name,
            "effect": "read_only",
            "retry_mode": "idempotent",
            "max_attempts": 2,
        }

        def is_read_only(self, arguments):
            return True

        async def execute(self, arguments, context):
            self.calls += 1
            return ToolResult("transient", is_error=True, retryable=True)

    tool, context = setup(tmp_path, monkeypatch, RetryRead())
    service = ToolExecutionService()
    service.contract = resolve_contract(tool, Input(value=1))
    await service.begin(context, tool, Input(value=1), "retry-limit", tmp_path)
    op = service.operation["operation_id"]
    service.ledger.settle(op, "failed", owner=service.owner)
    service.ledger.retry_failed(
        op, service.contract, verified_no_effect="read-only request, no writes"
    )
    result = await _execute_tool_call_impl(context, tool.name, "retry-limit", {"value": 1})
    assert result.is_error and tool.calls == 1
    with service.ledger.connect() as db:
        assert db.execute("SELECT attempts FROM operations").fetchone()[0] == 2
    with pytest.raises(ValueError):
        service.ledger.retry_failed(op, service.contract, verified_no_effect="read only")


@pytest.mark.asyncio
async def test_plugin_collision_cannot_replace_builtin_and_closes_transports(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from researchx import runtime

    monkeypatch.setenv("RESEARCHX_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("RESEARCHX_DATA_DIR", str(tmp_path / "data"))
    manager = SimpleNamespace(list_tools=lambda: [], close=AsyncMock())
    client = AsyncMock()
    monkeypatch.setattr(
        runtime,
        "load_plugins",
        lambda *args, **kwargs: [SimpleNamespace(enabled=True, tools=[FileWriteTool()])],
    )
    monkeypatch.setattr(runtime, "load_mcp_server_configs", lambda *args: {})
    monkeypatch.setattr(runtime, "McpClientManager", lambda *args: manager)
    with pytest.raises(ValueError, match="Duplicate/protected"):
        await runtime.build_runtime(
            cwd=str(tmp_path),
            session_id="a" * 12,
            api_client=client,
            connect_mcp=False,
            settings_override=Settings(),
        )
    manager.close.assert_awaited_once()
    client.close.assert_awaited_once()
