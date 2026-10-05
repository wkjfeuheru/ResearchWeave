"""Tests for MCP client error handling on disconnected servers."""

from __future__ import annotations

from pathlib import Path
import asyncio
from contextlib import AsyncExitStack
from unittest.mock import AsyncMock, MagicMock

import anyio
import pytest
from mcp.types import CallToolResult, TextContent

try:
    BaseExceptionGroup
except NameError:  # pragma: no cover - Python < 3.11 compatibility
    from exceptiongroup import BaseExceptionGroup

from openharness.mcp.client import McpClientManager, McpServerNotConnectedError, McpToolReturnedError
from openharness.mcp.types import McpConnectionStatus, McpStdioServerConfig, McpToolInfo
from openharness.tools.base import ToolExecutionContext
from openharness.tools.mcp_tool import McpToolAdapter
from openharness.tools.read_mcp_resource_tool import ReadMcpResourceTool


class _AsyncContextManager:
    def __init__(self, value):
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, exc_type, exc, tb):
        return False


# --- McpClientManager.call_tool ---


@pytest.mark.asyncio
async def test_call_tool_raises_when_server_never_connected():
    manager = McpClientManager({})
    with pytest.raises(McpServerNotConnectedError, match="not connected"):
        await manager.call_tool("missing", "some_tool", {})


@pytest.mark.asyncio
async def test_call_tool_raises_when_server_failed_to_connect():
    config = McpStdioServerConfig(command="false", args=[])
    manager = McpClientManager({"bad": config})
    manager._statuses["bad"] = McpConnectionStatus(
        name="bad", state="failed", detail="Connection refused",
    )
    with pytest.raises(McpServerNotConnectedError, match="Connection refused"):
        await manager.call_tool("bad", "tool", {})


@pytest.mark.asyncio
async def test_call_tool_raises_when_session_errors():
    manager = McpClientManager({})
    mock_session = AsyncMock()
    mock_session.call_tool.side_effect = RuntimeError("transport closed")
    manager._sessions["flaky"] = mock_session

    with pytest.raises(McpServerNotConnectedError, match="transport closed"):
        await manager.call_tool("flaky", "tool", {})


@pytest.mark.asyncio
async def test_call_tool_includes_unknown_server_detail_for_unconfigured():
    """When the server name is not even in _statuses, detail says 'unknown server'."""
    manager = McpClientManager({})
    with pytest.raises(McpServerNotConnectedError, match="unknown server"):
        await manager.call_tool("ghost", "tool", {})


# --- McpClientManager.read_resource ---


@pytest.mark.asyncio
@pytest.mark.parametrize("resource", [False, True])
async def test_request_timeout_cancels_wait_and_connection_can_be_reused(resource):
    manager = McpClientManager({"slow": McpStdioServerConfig(command="test", request_timeout=0.02)})
    session = AsyncMock()
    cancelled = asyncio.Event()

    async def hang(*args):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    method = session.read_resource if resource else session.call_tool
    method.side_effect = hang
    manager._sessions["slow"] = session
    expected = McpServerNotConnectedError if resource else McpToolReturnedError
    with pytest.raises(expected, match="timed out after 0.02s"):
        if resource:
            await manager.read_resource("slow", "data://test")
        else:
            await manager.call_tool("slow", "test", {})
    assert cancelled.is_set()
    session.call_tool.side_effect = None
    session.call_tool.return_value = CallToolResult(content=[TextContent(type="text", text="recovered")])
    assert await manager.call_tool("slow", "test", {}) == "recovered"


@pytest.mark.asyncio
async def test_tool_request_external_cancellation_is_not_reported_as_timeout():
    manager = McpClientManager({})
    session = AsyncMock()
    session.call_tool.side_effect = asyncio.CancelledError()
    manager._sessions["test"] = session
    with pytest.raises(asyncio.CancelledError):
        await manager.call_tool("test", "test", {})


@pytest.mark.asyncio
async def test_read_resource_raises_when_server_never_connected():
    manager = McpClientManager({})
    with pytest.raises(McpServerNotConnectedError, match="not connected"):
        await manager.read_resource("missing", "res://data")


@pytest.mark.asyncio
async def test_read_resource_raises_when_session_errors():
    manager = McpClientManager({})
    mock_session = AsyncMock()
    mock_session.read_resource.side_effect = OSError("broken pipe")
    manager._sessions["flaky"] = mock_session

    with pytest.raises(McpServerNotConnectedError, match="broken pipe"):
        await manager.read_resource("flaky", "res://data")


@pytest.mark.asyncio
async def test_register_connected_session_tolerates_missing_resources_list():
    manager = McpClientManager({})
    session = AsyncMock()
    session.initialize.return_value = None
    session.list_tools.return_value.tools = []
    session.list_resources.side_effect = RuntimeError("Method not found")
    stack = AsyncExitStack()
    await stack.__aenter__()
    stack.enter_async_context = AsyncMock(return_value=session)

    await manager._register_connected_session(
        name="context7",
        config=McpStdioServerConfig(command="npx", args=[]),
        stack=stack,
        read_stream=object(),
        write_stream=object(),
        auth_configured=False,
    )

    assert manager._statuses["context7"].state == "connected"
    assert manager._statuses["context7"].resources == []


@pytest.mark.asyncio
async def test_close_suppresses_known_runtime_error_from_stdio_cleanup():
    manager = McpClientManager({})
    stack = MagicMock()
    stack.aclose = AsyncMock(side_effect=RuntimeError("Attempted to exit cancel scope in a different task than it was entered in"))
    manager._stacks["context7"] = stack
    manager._sessions["context7"] = AsyncMock()

    await manager.close()

    assert manager._stacks == {}
    assert manager._sessions == {}


@pytest.mark.asyncio
async def test_close_suppresses_cancelled_error_from_stdio_cleanup():
    manager = McpClientManager({})
    stack = MagicMock()
    stack.aclose = AsyncMock(side_effect=asyncio.CancelledError())
    manager._stacks["context7"] = stack
    manager._sessions["context7"] = AsyncMock()

    await manager.close()

    assert manager._stacks == {}
    assert manager._sessions == {}


@pytest.mark.asyncio
async def test_close_failed_stack_suppresses_base_exception_group_cleanup_error():
    manager = McpClientManager({})
    stack = MagicMock()
    stack.aclose = AsyncMock(
        side_effect=BaseExceptionGroup(
            "cleanup failed",
            [asyncio.CancelledError()],
        )
    )

    await manager._close_failed_stack(stack)


@pytest.mark.asyncio
async def test_connect_all_marks_http_server_failed_when_initialize_is_cancelled(monkeypatch):
    import openharness.mcp.client as client_module
    from openharness.mcp.types import McpHttpServerConfig

    manager = McpClientManager(
        {
            "broken-http": McpHttpServerConfig(
                url="http://127.0.0.1:9999/mcp",
                headers={},
            )
        }
    )

    monkeypatch.setattr(
        client_module.httpx,
        "AsyncClient",
        lambda *args, **kwargs: _AsyncContextManager(AsyncMock()),
    )
    monkeypatch.setattr(
        client_module,
        "streamable_http_client",
        lambda *args, **kwargs: _AsyncContextManager((object(), object(), AsyncMock())),
    )
    manager._register_connected_session = AsyncMock(
        side_effect=asyncio.CancelledError("simulated cancellation")
    )

    await manager.connect_all()

    status = manager.list_statuses()[0]
    assert status.name == "broken-http"
    assert status.state == "failed"
    assert "simulated cancellation" in status.detail


# --- McpToolAdapter catches error and returns ToolResult(is_error=True) ---


@pytest.mark.asyncio
async def test_mcp_tool_adapter_returns_error_result_on_disconnected_server():
    manager = McpClientManager({})
    tool_info = McpToolInfo(
        server_name="gone",
        name="hello",
        description="test",
        input_schema={"type": "object", "properties": {"x": {"type": "string"}}},
    )
    adapter = McpToolAdapter(manager, tool_info)
    result = await adapter.execute(
        adapter.input_model.model_validate({"x": "1"}),
        ToolExecutionContext(cwd=Path(".")),
    )
    assert result.is_error is True
    assert "not connected" in result.output


# --- ReadMcpResourceTool catches error and returns ToolResult(is_error=True) ---


@pytest.mark.asyncio
async def test_read_mcp_resource_tool_returns_error_result_on_disconnected_server():
    manager = McpClientManager({})
    tool = ReadMcpResourceTool(manager)
    result = await tool.execute(
        tool.input_model.model_validate({"server": "gone", "uri": "res://x"}),
        ToolExecutionContext(cwd=Path(".")),
    )
    assert result.is_error is True
    assert "not connected" in result.output


@pytest.mark.asyncio
async def test_mcp_error_flag_remains_an_error_in_tool_adapter():
    manager = McpClientManager({})
    session = AsyncMock()
    session.call_tool.return_value = CallToolResult(
        content=[TextContent(type="text", text="Upstream document unavailable")], isError=True,
    )
    manager._sessions["reports"] = session
    adapter = McpToolAdapter(manager, McpToolInfo(
        server_name="reports", name="fetch", description="Read a report", input_schema={"type": "object"},
    ))
    result = await adapter.execute(adapter.input_model(), ToolExecutionContext(cwd=Path(".")))
    assert result.is_error and "unavailable" in result.output


@pytest.mark.asyncio
async def test_close_multiple_transport_scopes_does_not_cancel_next_run():
    manager = McpClientManager({})
    for name in ("outer-transport", "inner-transport"):
        stack = AsyncExitStack()
        group = await stack.enter_async_context(anyio.create_task_group())
        stack.callback(group.cancel_scope.cancel)
        manager._stacks[name] = stack
    await manager.close()
    await asyncio.sleep(0)
    assert not manager._stacks
