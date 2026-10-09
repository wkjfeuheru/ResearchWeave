"""Higher-level system prompt assembly."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from openharness.config.settings import Settings
from openharness.permissions.modes import PermissionMode
from openharness.prompts.system_prompt import get_base_system_prompt, _format_environment_section
from openharness.prompts.environment import get_environment_info
from openharness.skills.loader import load_skill_registry
from typing_extensions import TypedDict, Unpack
from openharness.engine.messages import ContextSpan


def _build_skills_section(
    cwd: str | Path,
    *,
    extra_skill_dirs: Iterable[str | Path] | None = None,
    extra_plugin_roots: Iterable[str | Path] | None = None,
    settings: Settings | None = None,
) -> str | None:
    """Build a system prompt section listing available skills."""
    registry = load_skill_registry(
        cwd,
        extra_skill_dirs=extra_skill_dirs,
        extra_plugin_roots=extra_plugin_roots,
        settings=settings,
    )
    skills = [
        skill
        for skill in registry.list_skills()
        if not skill.disable_model_invocation and skill.metadata.status in {"active", "deprecated"}
    ]
    if not skills:
        return None
    lines = [
        "# Available Skills",
        "",
        "The following skills are available via the `skill` tool. "
        'When a user\'s request matches a skill, invoke it with `skill(name="<skill_name>")` '
        "to load detailed instructions before proceeding. "
        "Users can enable skills from the Web workspace.",
        "",
    ]
    for skill in sorted(skills, key=lambda item: item.command_name or item.name):
        command_name = skill.command_name or skill.name
        display = f" ({skill.display_name})" if skill.display_name else ""
        lines.append(f"- **{command_name}**{display}: {skill.description}")
        if skill.metadata.status == "deprecated":
            lines.append(
                f"  Deprecated: {skill.metadata.deprecation or 'No replacement specified'}"
            )
    return "\n".join(lines)


def _build_permission_mode_section(settings: Settings) -> str:
    """Build current permission-mode guidance for the model."""
    mode = settings.permission.mode
    if mode == PermissionMode.PLAN:
        guidance = (
            "Plan mode is enabled. Treat this session as read-only planning and analysis. "
            "Do not call mutating tools such as file writes, edits, package installs, "
            "state-changing shell commands, or task-spawning actions unless the user exits plan mode."
        )
    elif mode == PermissionMode.FULL_AUTO:
        guidance = (
            "Full-auto permission mode is enabled. You may use mutating tools when they are necessary "
            "for the user's request, while still keeping changes scoped and intentional."
        )
    else:
        guidance = (
            "Default permission mode is enabled. Read-only tools can run directly; mutating tools "
            "may require explicit user approval."
        )
    return f"# Current Permission Mode\n{guidance}"


@dataclass(frozen=True)
class RuntimePrompt:
    system_prompt: str
    runtime_context: str
    runtime_context_manifest: list[ContextSpan] | None = None


def build_runtime_prompt(
    settings: Settings,
    *,
    cwd: str | Path,
    latest_user_prompt: str | None = None,
    extra_skill_dirs: Iterable[str | Path] | None = None,
    extra_plugin_roots: Iterable[str | Path] | None = None,
) -> RuntimePrompt:
    """Keep instruction/catalog prefixes stable and snapshot per-turn reference state."""
    sections = [settings.system_prompt or get_base_system_prompt()]

    dynamic = [_format_environment_section(get_environment_info(cwd=str(cwd)))]
    dynamic.append(
        "<permission_mode>\n" + _build_permission_mode_section(settings) + "\n</permission_mode>"
    )

    if settings.fast_mode:
        dynamic.append(
            "# Session Mode\nFast mode is enabled. Prefer concise replies, minimal tool use, and quicker progress over exhaustive exploration."
        )

    dynamic.append(
        "# Reasoning Settings\n"
        f"- Effort: {settings.effort}\n"
        f"- Passes: {settings.passes}\n"
        "Adjust depth and iteration count to match these settings while still completing the task."
    )

    skills_section = _build_skills_section(
        cwd,
        extra_skill_dirs=extra_skill_dirs,
        extra_plugin_roots=extra_plugin_roots,
        settings=settings,
    )
    if skills_section:
        sections.append(skills_section)

    if settings.research_memory.enabled:
        from openharness.research.prompt import RESEARCH_MEMORY_PROMPT

        sections.append(RESEARCH_MEMORY_PROMPT)

    from openharness.services.context_sources import ContextSnapshot

    snapshot = ContextSnapshot.join(
        [
            (text, "dynamic_context" if index == 0 else "system_prompt", "runtime", index == 0)
            for index, text in enumerate(dynamic)
            if text.strip()
        ]
    )
    return RuntimePrompt(
        system_prompt="\n\n".join(section for section in sections if section.strip()),
        runtime_context=snapshot.text,
        runtime_context_manifest=snapshot.manifest,
    )


class PromptOptions(TypedDict, total=False):
    cwd: str
    latest_user_prompt: str | None
    extra_skill_dirs: Iterable[str | Path] | None
    extra_plugin_roots: Iterable[str | Path] | None


def build_runtime_system_prompt(settings: Settings, **kwargs: Unpack[PromptOptions]) -> str:
    """Legacy combined prompt for previews and external integrations."""
    prompt = build_runtime_prompt(settings, **kwargs)
    return prompt.system_prompt + "\n\n" + prompt.runtime_context
