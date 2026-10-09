"""Plugin runtime types."""

from __future__ import annotations

from pydantic import BaseModel
from openharness.hooks.schemas import HookDefinition
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from openharness.mcp.types import McpServerConfig
from openharness.plugins.schemas import PluginManifest
from openharness.skills.types import SkillDefinition

if TYPE_CHECKING:
    from openharness.tools.base import BaseTool


@dataclass(frozen=True)
class LoadedPlugin:
    """A loaded plugin and its contributed artifacts."""

    manifest: PluginManifest
    path: Path
    enabled: bool
    skills: list[SkillDefinition] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)
    tools: list[BaseTool[BaseModel]] = field(default_factory=list)
    hooks: dict[str, list[HookDefinition]] = field(default_factory=dict)
    mcp_servers: dict[str, McpServerConfig] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.manifest.name

    @property
    def description(self) -> str:
        return self.manifest.description
