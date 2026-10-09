"""Load MCP server config from settings and plugins."""

from __future__ import annotations

from researchx.config import Settings
from researchx.mcp.types import McpServerConfig
from researchx.plugins.types import LoadedPlugin


def load_mcp_server_configs(
    settings: Settings, plugins: list[LoadedPlugin]
) -> dict[str, McpServerConfig]:
    """Merge settings and plugin MCP server configs."""
    servers = dict(settings.mcp_servers)
    for plugin in plugins:
        if not plugin.enabled:
            continue
        for name, config in plugin.mcp_servers.items():
            servers.setdefault(f"{plugin.manifest.name}:{name}", config)
    return servers
