"""Plugin manifest schemas."""

from __future__ import annotations

from pydantic import BaseModel


class PluginManifest(BaseModel):
    """Plugin manifest stored in plugin.json or .claude-plugin/plugin.json."""

    name: str
    version: str = "0.0.0"
    description: str = ""
    display_name: str | None = None
    category: str = "社区技能"
    example: str = ""
    enabled_by_default: bool = True
    skills_dir: str = "skills"
    tools_dir: str = "tools"
    hooks_file: str = "hooks.json"
    mcp_file: str = "mcp.json"
    # Extended fields: optional author, commands, agents, etc.
    author: dict[str, object] | None = None
    commands: str | list[object] | dict[str, object] | None = None
    agents: str | list[object] | None = None
    skills: str | list[object] | None = None
    hooks: str | dict[str, object] | list[object] | None = None
