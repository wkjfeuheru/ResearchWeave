"""Retired registrations and actual MCP delivery boundaries use the same receipts."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel

from openharness.engine.query import _execute_tool_call_impl
from openharness.mcp.client import McpClientManager, McpServerNotConnectedError
from openharness.mcp.types import McpStdioServerConfig, McpToolInfo
from openharness.tools.base import BaseTool, ToolRegistry, ToolResult
from openharness.tools.mcp_tool import McpToolAdapter
from openharness.tools.retired import RETIRED_TOOL_NAMES
from tests.test_harness.test_execution import setup, ledger


@pytest.mark.parametrize("name", sorted(RETIRED_TOOL_NAMES))
@pytest.mark.parametrize("replace", [False, True])
def test_retired_tools_cannot_be_reintroduced_by_plugin(name, replace):
    class Plugin(BaseTool):
        description = "retired plugin alias"
        input_model = BaseModel

        async def execute(self, arguments, context):
            return ToolResult("must not run")

    tool = Plugin()
    tool.name = name
    registry = ToolRegistry()
    with pytest.raises(ValueError, match="Retired tool"):
        registry.register(tool, replace=replace)
    assert registry.list_tools() == [] and registry.to_api_schema() == []


def adapter(manager):
    return McpToolAdapter(
        manager,
        McpToolInfo(
            "demo", "external_post", "Write remote state", {"type": "object", "properties": {}}
        ),
    )


async def test_disconnected_mcp_proves_request_not_sent(tmp_path, monkeypatch):
    manager = McpClientManager({})
    with pytest.raises(McpServerNotConnectedError) as exc:
        await manager.call_tool("demo", "external_post", {})
    assert exc.value.request_not_sent
    tool, context = setup(tmp_path, monkeypatch, tool=adapter(manager))
    result = await _execute_tool_call_impl(context, tool.name, "not-sent", {})
    assert result.is_error and result.result_metadata["no_effect"] is True
    assert result.result_metadata["status"] == "failed"
    row = ledger(tmp_path).get(
        result.result_metadata["operation_id"], session="session", scope=str(tmp_path)
    )
    assert row["status"] == "failed" and row["attempts"] == 1
    assert tool.contract.get("retry_mode", "never") == "never"


@pytest.mark.parametrize("failure", ["timeout", "lost_transport", "remote_error"])
async def test_mcp_after_dispatch_uncertain_survives_restart_without_replay(
    tmp_path, monkeypatch, failure
):
    from mcp.types import CallToolResult

    manager = McpClientManager(
        {"demo": McpStdioServerConfig(command="unused", request_timeout=0.02)}
    )
    session = AsyncMock()
    entered = asyncio.Event()

    async def external_write(*args, **kwargs):
        with (tmp_path / "remote_effect").open("a") as out:
            out.write("posted\n")
        entered.set()
        if failure == "timeout":
            await asyncio.sleep(30)
        if failure == "lost_transport":
            raise RuntimeError("closed after request")
        return CallToolResult(content=[], isError=True)

    session.call_tool.side_effect = external_write
    manager._sessions["demo"] = session
    tool, context = setup(tmp_path, monkeypatch, tool=adapter(manager))
    first = await _execute_tool_call_impl(context, tool.name, "external-write", {})
    assert entered.is_set() and first.is_error
    assert first.result_metadata["status"] == "uncertain"
    assert first.result_metadata["no_effect"] is False
    restarted, restored = setup(tmp_path, monkeypatch, tool=adapter(manager))
    replay = await _execute_tool_call_impl(restored, restarted.name, "external-write", {})
    other_call = await _execute_tool_call_impl(restored, restarted.name, "new-id-same-resource", {})
    assert replay.is_error and other_call.is_error
    assert session.call_tool.await_count == 1
    assert (tmp_path / "remote_effect").read_text() == "posted\n"
    row = ledger(tmp_path).get(
        first.result_metadata["operation_id"], session="session", scope=str(tmp_path)
    )
    assert row["status"] == "uncertain" and row["attempts"] == 1


async def test_reconciliation_requires_matching_success_artifact_before_reuse(
    tmp_path, monkeypatch
):
    from openharness.engine.messages import ToolResultBlock
    from pathlib import Path

    manager = McpClientManager({"demo": McpStdioServerConfig(command="unused")})
    session = AsyncMock()
    session.call_tool.side_effect = RuntimeError("closed after dispatch")
    manager._sessions["demo"] = session
    tool, context = setup(tmp_path, monkeypatch, tool=adapter(manager))
    uncertain = await _execute_tool_call_impl(context, tool.name, "manual-review", {})
    operation = uncertain.result_metadata["operation_id"]
    store = ledger(tmp_path)
    # A host has verified the remote success, but the old artifact is still an error.
    store.settle(operation, "succeeded", evidence="Remote audit confirmed commit")
    blocked = await _execute_tool_call_impl(context, tool.name, "manual-review", {})
    assert blocked.is_error and blocked.result_metadata["error_code"] == "artifact_unavailable"
    row = store.get(operation, session="session", scope=str(tmp_path))
    path = Path(row["result_ref"])
    restored = ToolResultBlock(
        tool_use_id="manual-review",
        content="Remote audit: committed",
        result_metadata={"status": "success", "operation_id": "wrong-operation"},
    )
    path.write_text(restored.model_dump_json())
    wrong = await _execute_tool_call_impl(context, tool.name, "manual-review", {})
    assert wrong.is_error and wrong.result_metadata["status"] == "blocked"
    restored.result_metadata["operation_id"] = operation
    path.write_text(restored.model_dump_json())
    reused = await _execute_tool_call_impl(context, tool.name, "manual-review", {})
    assert not reused.is_error and reused.content == "Remote audit: committed"
    assert reused.result_metadata["replayed_receipt"]
    assert session.call_tool.await_count == 1
