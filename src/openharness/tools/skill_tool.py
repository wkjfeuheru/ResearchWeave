"""Tool for reading skill contents."""

from __future__ import annotations

from pathlib import Path
import sys

from pydantic import BaseModel, Field

from openharness.skills import load_skill_registry
from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult


class SkillToolInput(BaseModel):
    """Arguments for skill lookup."""

    name: str = Field(description="Skill name")


class SkillTool(BaseTool):
    """Return the content of a loaded skill."""

    name = "skill"
    description = "Read an enabled plugin, user, or project skill and locate its resources. Supporting files are read on demand, never automatically executed."
    input_model = SkillToolInput

    def is_read_only(self, arguments: SkillToolInput) -> bool:
        del arguments
        return True

    async def execute(self, arguments: SkillToolInput, context: ToolExecutionContext) -> ToolResult:
        registry = load_skill_registry(
            context.cwd,
            extra_skill_dirs=context.metadata.get("extra_skill_dirs"),
            extra_plugin_roots=context.metadata.get("extra_plugin_roots"),
            settings=context.metadata.get("skill_settings"),
        )
        skill = registry.get(arguments.name) or registry.get(arguments.name.lower()) or registry.get(arguments.name.title())
        if skill is None:
            return ToolResult(output=f"Skill not found: {arguments.name}", is_error=True)
        if skill.metadata.status in {"draft", "retired"}:
            return ToolResult(output=f"Skill {skill.name} is {skill.metadata.status} and cannot be executed.", is_error=True)
        if skill.disable_model_invocation:
            command_name = skill.command_name or skill.name
            return ToolResult(
                output=f"Skill {command_name} can only be invoked by the user as /{command_name}.",
                is_error=True,
            )
        paths = [f"Skill status: {skill.metadata.status}", f"Python interpreter: {sys.executable}"]
        if skill.metadata.deprecation:
            paths.append(f"Deprecation: {skill.metadata.deprecation}")
        store = context.metadata.get("research_store")
        if store is not None:
            paths.append(f"Current session output directory: {store.directory / 'work'}")
        if skill.path:
            paths.append(f"Skill entrypoint: {skill.path}")
        if skill.base_dir:
            paths.append(f"Resource base directory: {skill.base_dir}")
            base = Path(skill.base_dir).resolve()
            for directory in ("references", "scripts", "templates", "assets"):
                root = base / directory
                if not root.is_dir() or not root.resolve().is_relative_to(base):
                    continue
                paths.append(f"- {directory}/: {root.resolve()}")
                for path in sorted(root.rglob("*")):
                    resolved = path.resolve()
                    if path.is_file() and not path.name.startswith(".") and resolved.is_relative_to(base):
                        paths.append(f"- {path.relative_to(base)}: {resolved}")
            paths.append("Read supporting files only when the current step needs them, using read_file and the absolute paths above. "
                         "Scripts and templates are resources; listing them does not execute or load them.")
        return ToolResult(output="\n".join(paths) + "\n\n" + skill.content,
                          metadata={"skill_entrypoint": skill.path, "skill_base_dir": skill.base_dir,
                                    "skill_metadata": skill.metadata.model_dump()})
