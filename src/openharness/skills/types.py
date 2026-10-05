"""Skill data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field


class SkillMetadata(BaseModel):
    """Portable lifecycle and capability declarations; permissions remain enforced by tools."""

    skill_id: str = ""
    version: str = "0.1.0"
    owner: str = "未声明"
    permissions: list[str] = Field(default_factory=list)
    required_tools: list[str] = Field(default_factory=list)
    optional_tools: list[str] = Field(default_factory=list)
    compatible_models: list[str] = Field(default_factory=lambda: ["text", "tool_calling"])
    scope: str = "current_conversation"
    status: Literal["draft", "active", "deprecated", "retired"] = "active"
    published_at: str | None = None
    deprecation: dict[str, str] | None = None
    content_hash: str = ""


@dataclass(frozen=True)
class SkillDefinition:
    """A loaded skill."""

    name: str
    description: str
    content: str
    source: str
    path: str | None = None
    base_dir: str | None = None
    command_name: str | None = None
    display_name: str | None = None
    aliases: tuple[str, ...] = ()
    user_invocable: bool = True
    disable_model_invocation: bool = False
    model: str | None = None
    argument_hint: str | None = None
    metadata: SkillMetadata = field(default_factory=SkillMetadata)
