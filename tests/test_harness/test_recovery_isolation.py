"""Unresolved effects retain write safety without disabling diagnostics/recovery."""

import asyncio
import json
import os
import subprocess
import sys

import pytest
from sqlalchemy import update

from researchx.engine.query import _execute_tool_call_impl
from researchx.engine.messages import ToolResultBlock
from researchx.storage.content import persist_object
from researchx.services.execution.operations import OperationStore
from researchx.storage import schema as s
from researchx.tools.base import BaseTool, ToolResult
from researchx.tools.file_write_tool import FileWriteTool
from researchx.tools.file_read_tool import FileReadTool
from researchx.tools.research_memory_tool import ResearchMemoryTool, ResearchMemoryInput
from researchx.tools.contracts import resolve_contract
from tests.test_harness.test_execution import Input, Write, setup


class Diagnostic(BaseTool[Input]):
    name = "diagnostic_probe"
    description = "Read only"
    input_model = Input
    contract = {"name": name, "effect": "read_only"}

    async def execute(self, arguments, context):
        return ToolResult("diagnostic remains available")

    def is_read_only(self, arguments):
        return True


async def orphan(store, *, session="session", call="orphan", resources=None):
    row = await store.prepare(
        session=session,
        scope=store.cwd,
        run=call,
        call=call,
        tool="bash",
        version="1",
        digest="a" * 64,
        effect="unknown",
        resources=resources or {"read": [], "write": ["*"]},
    )
    await store.claim(row["operation_id"], "dead-owner")
    async with store.database.transaction() as db:
        await db.execute(
            update(s.tool_operations)
            .where(s.tool_operations.c.operation_id == row["operation_id"])
            .values(pid=99999999)
        )
    return row["operation_id"]


async def test_crash_read_diagnosis_reconciliation_then_same_prepared_call_runs(
    tmp_path, monkeypatch
):
    store = OperationStore(tmp_path)
    partial = tmp_path / "partial"
    partial.write_text("write started before process loss")
    op = await orphan(store)
    tool, ctx = setup(tmp_path, monkeypatch, tool=Diagnostic())
    read = await _execute_tool_call_impl(ctx, tool.name, "diagnose", {"value": 1})
    assert not read.is_error  # Recovery marked the old write uncertain, never replayed it.
    assert (await store.get(op, session="session", scope=str(tmp_path)))["status"] == "uncertain"
    writer, ctx = setup(tmp_path, monkeypatch, tool=Write())
    blocked = await _execute_tool_call_impl(ctx, writer.name, "next-write", {"value": 42})
    assert blocked.result_metadata["conflicting_operations"] == [op]
    assert "rx operations resolve" in blocked.content and writer.calls == 0
    next_op = blocked.result_metadata["operation_id"]
    assert (await store.get(next_op, session="session", scope=str(tmp_path)))[
        "status"
    ] == "prepared"
    with pytest.raises(ValueError, match="another session"):
        await store.reconcile(op, session="wrong", outcome="no_effect", evidence="review")
    with pytest.raises(ValueError, match="evidence"):
        await store.reconcile(op, session="session", outcome="no_effect", evidence=" ")
    with pytest.raises(ValueError, match="receipt"):
        await store.reconcile(op, session="session", outcome="succeeded", evidence="review")
    partial.unlink()  # Explicitly compensate the observed local effect before release.
    await store.reconcile(
        op,
        session="session",
        outcome="no_effect",
        evidence="Removed partial file; verified no remote writes",
    )
    resumed = await _execute_tool_call_impl(ctx, writer.name, "next-write", {"value": 42})
    assert not resumed.is_error and writer.calls == 1
    row = await store.get(op, session="session", scope=str(tmp_path))
    assert row["status"] == "failed" and row["attempts"] == 1
    assert json.loads(row["reconciliation"])["outcome"] == "no_effect"
    with pytest.raises(ValueError, match="Only unresolved"):
        await store.reconcile(op, session="session", outcome="no_effect", evidence="again")


async def test_uncertain_remote_server_does_not_reserve_local_files(tmp_path, monkeypatch):
    store = OperationStore(tmp_path)
    op = await orphan(store, resources={"read": [], "write": ["mcp:server-a"]})
    await store.recover(session="session", scope=str(tmp_path))
    writer, ctx = setup(tmp_path, monkeypatch, tool=FileWriteTool())
    result = await _execute_tool_call_impl(
        ctx, writer.name, "local-write", {"path": "note.md", "content": "independent"}
    )
    assert not result.is_error and (tmp_path / "note.md").read_text() == "independent"
    assert (await store.get(op, session="session", scope=str(tmp_path)))["status"] == "uncertain"


async def test_operator_cli_can_inspect_and_resolve_without_replaying(tmp_path):
    store = OperationStore(tmp_path)
    op = await orphan(store)
    await store.recover(session="session", scope=str(tmp_path))

    def invoke(*args):
        return subprocess.run(
            [sys.executable, "-m", "researchx", "operations", *args],
            env=dict(os.environ),
            capture_output=True,
            text=True,
            timeout=30,
        )

    listed = await asyncio.to_thread(invoke, "list", "--cwd", str(tmp_path))
    assert listed.returncode == 0, listed.stderr
    assert json.loads(listed.stdout)[0]["operation_id"] == op
    resolved = await asyncio.to_thread(
        invoke,
        "resolve",
        op,
        "--cwd",
        str(tmp_path),
        "--session",
        "session",
        "--outcome",
        "no_effect",
        "--evidence",
        "Verified transaction never committed",
    )
    assert resolved.returncode == 0, resolved.stderr
    row = await store.get(op, session="session", scope=str(tmp_path))
    assert row["status"] == "failed" and row["attempts"] == 1


def test_memory_read_contract_is_explicit_but_mutations_still_write():
    tool = ResearchMemoryTool()
    read = ResearchMemoryInput.model_validate({"operation": {"action": "read"}})
    assert resolve_contract(tool, read).effect == "read_only"
    assert resolve_contract(tool).effect == "local_write"


async def test_verified_success_receipt_is_reused_without_external_write_replay(
    tmp_path, monkeypatch
):
    tool, ctx = setup(tmp_path, monkeypatch)
    tool.wait = True
    result = await _execute_tool_call_impl(ctx, tool.name, "lost-response", {"value": 42})
    assert result.result_metadata["status"] == "uncertain" and tool.calls == 1
    store = OperationStore(tmp_path)
    op = result.result_metadata["operation_id"]
    receipt = ToolResultBlock(
        tool_use_id="wrong-call",
        content="Verified remote commit",
        result_metadata={"status": "success", "operation_id": op},
    )
    ref = await persist_object(store.database, store.workspace, receipt.model_dump_json().encode())
    with pytest.raises(ValueError, match="tool call"):
        await store.reconcile(
            op, session="session", outcome="succeeded", evidence="Remote audit", result_ref=ref
        )
    receipt.tool_use_id = "lost-response"
    ref = await persist_object(store.database, store.workspace, receipt.model_dump_json().encode())
    await store.reconcile(
        op,
        session="session",
        outcome="succeeded",
        evidence="Remote audit confirmed commit",
        result_ref=ref,
    )
    reused = await _execute_tool_call_impl(ctx, tool.name, "lost-response", {"value": 42})
    assert not reused.is_error and reused.result_metadata["replayed_receipt"] and tool.calls == 1


async def test_read_tool_with_effect_hook_cannot_bypass_unresolved_write(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock
    from researchx.config import Settings
    from researchx.hooks import HookEvent, HookExecutor, HookExecutionContext
    from researchx.hooks.loader import HookRegistry
    from researchx.hooks.schemas import CommandHookDefinition

    store = OperationStore(tmp_path)
    op = await orphan(store, resources={"read": [], "write": [str(tmp_path / "forbidden_hook")]})
    await store.recover(session="session", scope=str(tmp_path))
    registry = HookRegistry()
    registry.register(HookEvent.PRE_TOOL_USE, CommandHookDefinition(command="touch forbidden_hook"))
    hooks = HookExecutor(
        registry, HookExecutionContext(tmp_path, AsyncMock(), "test", settings=Settings())
    )
    (tmp_path / "independent.txt").write_text("diagnostic")
    tool, ctx = setup(tmp_path, monkeypatch, tool=FileReadTool(), hooks=hooks)
    blocked = await _execute_tool_call_impl(
        ctx, tool.name, "read-with-effect", {"path": "independent.txt"}
    )
    assert blocked.result_metadata["error_code"] == "reconciliation_required"
    assert blocked.result_metadata["conflicting_operations"] == [op]
    assert not (tmp_path / "forbidden_hook").exists()
