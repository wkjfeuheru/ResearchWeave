"""MCP credentials are host configuration, never a model-callable auth tool."""

from pathlib import Path
from unittest.mock import AsyncMock
from openharness.config.settings import Settings, load_settings, save_settings
from openharness.mcp.client import McpClientManager
from openharness.mcp.types import McpHttpServerConfig, McpStdioServerConfig
from openharness.tools import create_research_tool_registry


async def test_host_updates_http_headers_and_reconnects(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    settings = Settings(mcp_servers={"demo": McpHttpServerConfig(url="https://example.com/mcp")})
    save_settings(settings)
    manager = McpClientManager(settings.mcp_servers)
    connect, close = AsyncMock(), AsyncMock()
    monkeypatch.setattr(manager, "connect_all", connect)
    monkeypatch.setattr(manager, "close", close)
    settings.mcp_servers["demo"].headers["Authorization"] = "Bearer secret"
    save_settings(settings)
    manager.update_server_config("demo", load_settings().mcp_servers["demo"])
    await manager.reconnect_all()
    assert load_settings().mcp_servers["demo"].headers["Authorization"] == "Bearer secret"
    assert manager.get_server_config("demo").headers["Authorization"] == "Bearer secret"
    assert close.await_count == connect.await_count == 1
    assert create_research_tool_registry().get("mcp_auth") is None


async def test_host_persists_stdio_env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    settings = Settings(
        mcp_servers={"fixture": McpStdioServerConfig(command="python", args=["-m", "fixture"])}
    )
    settings.mcp_servers["fixture"].env = {"FIXTURE_TOKEN": "abc123"}
    save_settings(settings)
    saved = load_settings().mcp_servers["fixture"]
    assert saved.env["FIXTURE_TOKEN"] == "abc123"
    assert saved.command == "python" and saved.args == ["-m", "fixture"]
    assert create_research_tool_registry(mode="general").get("mcp_auth") is None


async def test_host_can_persist_active_manager_config(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    save_settings(Settings())
    manager = McpClientManager(
        {"fixture": McpStdioServerConfig(command="python", args=["-m", "fixture"])}
    )
    config = manager.get_server_config("fixture").model_copy(deep=True)
    config.env = {"MCP_AUTH_TOKEN": "Bearer token-smoke"}
    settings = load_settings()
    settings.mcp_servers["fixture"] = config
    save_settings(settings)
    manager.update_server_config("fixture", config)
    monkeypatch.setattr(manager, "close", AsyncMock())
    monkeypatch.setattr(manager, "connect_all", AsyncMock())
    await manager.reconnect_all()
    assert load_settings().mcp_servers["fixture"].env["MCP_AUTH_TOKEN"] == "Bearer token-smoke"
    assert manager.connect_all.await_count == 1
    assert manager.get_server_config("fixture").env == config.env
