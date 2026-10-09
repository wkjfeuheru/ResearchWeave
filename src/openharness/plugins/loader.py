"""Plugin discovery and loading."""

from __future__ import annotations
from openharness.config import Settings
from openharness.hooks.schemas import HookDefinition
from openharness.mcp.types import McpServerConfig
from pydantic import BaseModel, TypeAdapter
from openharness.tools.base import BaseTool

import importlib
import importlib.util
import json
import logging
import os
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterable

import yaml

from openharness.config.paths import get_config_dir
from openharness.plugins.schemas import PluginManifest
from openharness.plugins.types import LoadedPlugin
from openharness.skills.loader import load_skills_from_dirs
from openharness.skills.types import SkillDefinition

logger = logging.getLogger(__name__)
BUNDLED_PLUGINS_DIR = Path(__file__).parent / "bundled"


def get_user_plugins_dir() -> Path:
    """Return the user plugin directory."""
    path = get_config_dir() / "plugins"
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_project_plugins_dir(cwd: str | Path) -> Path:
    """Return the project plugin directory."""
    path = Path(cwd).resolve() / ".openharness" / "plugins"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _find_manifest(plugin_dir: Path) -> Path | None:
    """Find plugin.json in standard or .claude-plugin/ locations."""
    for candidate in [
        plugin_dir / "plugin.json",
        plugin_dir / ".claude-plugin" / "plugin.json",
    ]:
        if candidate.exists() and candidate.resolve().is_relative_to(plugin_dir.resolve()):
            return candidate
    return None


def discover_plugin_paths(
    cwd: str | Path, extra_roots: Iterable[str | Path] | None = None
) -> list[Path]:
    """Find plugin directories from user and project locations."""
    roots = [BUNDLED_PLUGINS_DIR, get_user_plugins_dir(), get_project_plugins_dir(cwd)]
    if extra_roots:
        for root in extra_roots:
            path = Path(root).expanduser().resolve()
            path.mkdir(parents=True, exist_ok=True)
            roots.append(path)
    paths: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.iterdir()):
            if (
                path.is_dir()
                and path.resolve().is_relative_to(root.resolve())
                and _find_manifest(path) is not None
                and path not in seen
            ):
                seen.add(path)
                paths.append(path)
    return paths


def discover_plugin_paths_for_settings(
    settings: Settings,
    cwd: str | Path,
    extra_roots: Iterable[str | Path] | None = None,
) -> list[Path]:
    """Find plugin directories that are permitted by the active settings."""
    roots = [BUNDLED_PLUGINS_DIR, get_user_plugins_dir()]
    if getattr(settings, "allow_project_plugins", False):
        roots.append(get_project_plugins_dir(cwd))
    if extra_roots:
        for root in extra_roots:
            path = Path(root).expanduser().resolve()
            path.mkdir(parents=True, exist_ok=True)
            roots.append(path)
    paths: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.iterdir()):
            if (
                path.is_dir()
                and path.resolve().is_relative_to(root.resolve())
                and _find_manifest(path) is not None
                and path not in seen
            ):
                seen.add(path)
                paths.append(path)
    return paths


def load_plugins(
    settings: Settings,
    cwd: str | Path,
    extra_roots: Iterable[str | Path] | None = None,
    *,
    metadata_only: bool = False,
) -> list[LoadedPlugin]:
    """Load plugins from disk."""
    project_plugins_dir = get_project_plugins_dir(cwd)
    if not getattr(settings, "allow_project_plugins", False) and any(
        path.is_dir() and _find_manifest(path) is not None
        for path in sorted(project_plugins_dir.iterdir())
    ):
        logger.warning(
            "Found project-local plugins in %s, but they are disabled by default. "
            "Set allow_project_plugins=true if you trust this workspace.",
            project_plugins_dir,
        )
    plugins: dict[str, LoadedPlugin] = {}
    for path in discover_plugin_paths_for_settings(settings, cwd, extra_roots=extra_roots):
        plugin = load_plugin(
            path,
            settings.enabled_plugins,
            enabled_skills=getattr(settings, "enabled_skills", {}),
            metadata_only=metadata_only,
        )
        if plugin is not None:
            plugins[plugin.name] = plugin
    return list(plugins.values())


def load_plugin(
    path: Path,
    enabled_plugins: dict[str, bool],
    *,
    enabled_skills: dict[str, bool] | None = None,
    metadata_only: bool = False,
) -> LoadedPlugin | None:
    """Load one plugin directory."""
    if path.is_symlink():
        logger.warning("Ignoring symlink plugin entry: %s", path)
        return None
    manifest_path = _find_manifest(path)
    if manifest_path is None:
        return None
    try:
        manifest = PluginManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.debug("Failed to load plugin manifest from %s: %s", manifest_path, exc)
        return None
    for value in (
        manifest.skills_dir,
        manifest.tools_dir,
        manifest.hooks_file,
        manifest.mcp_file,
        "hooks/hooks.json",
        ".mcp.json",
    ):
        candidate = path / value
        if not candidate.resolve().is_relative_to(path.resolve()):
            logger.warning("Ignoring plugin contribution outside its root: %s", candidate)
            return None
    enabled = enabled_plugins.get(manifest.name, manifest.enabled_by_default)

    skills = _load_plugin_skills(path / manifest.skills_dir)
    skills = [
        replace(
            skill,
            plugin_name=manifest.name,
            enabled=enabled and skill_enabled(skill, enabled_plugins, enabled_skills or {}),
        )
        for skill in skills
    ]
    diagnostics = []
    for contribution in ("commands", "agents"):
        if getattr(manifest, contribution, None) or (path / contribution).exists():
            diagnostics.append(
                f"Retired {contribution} contributions are ignored; use research skills, tools, MCP or hooks."
            )
    tools = _load_plugin_tools(path, manifest) if enabled and not metadata_only else []
    hooks = _load_plugin_hooks(path / manifest.hooks_file) if not metadata_only else {}
    hooks_dir_file = path / "hooks" / "hooks.json"
    if not metadata_only and not hooks and hooks_dir_file.exists():
        hooks = _load_plugin_hooks_structured(hooks_dir_file, path)

    mcp = _load_plugin_mcp(path / manifest.mcp_file) if not metadata_only else {}
    mcp_json = path / ".mcp.json"
    if not metadata_only and not mcp and mcp_json.exists():
        mcp = _load_plugin_mcp(mcp_json)

    return LoadedPlugin(
        manifest=manifest,
        path=path,
        enabled=enabled,
        skills=skills,
        diagnostics=diagnostics,
        hooks=hooks,
        mcp_servers=mcp,
        tools=tools,
    )


LEGACY_SKILL_PLUGINS = {
    "financial-statement-analysis",
    "company-event-monitor",
    "research-report-digest",
    "deep-investment-report",
}


def skill_enabled(
    skill: SkillDefinition, enabled_plugins: dict[str, bool], enabled_skills: dict[str, bool]
) -> bool:
    """Explicit Skill settings take precedence over legacy one-Skill plugin settings."""
    key = skill.metadata.skill_id or skill.command_name or skill.name
    names = (key, skill.command_name, skill.name)
    for name in names:
        if name in enabled_skills:
            return enabled_skills[name]
    legacy = next((name for name in names if name in LEGACY_SKILL_PLUGINS), None)
    return enabled_plugins.get(legacy, True) if legacy else True


def _parse_frontmatter(content: str, path: Path) -> tuple[dict[str, Any], str]:
    if not content.startswith("---\n"):
        return {}, content
    marker = "\n---\n"
    end_index = content.find(marker, 4)
    if end_index == -1:
        return {}, content
    raw_frontmatter = content[4:end_index]
    body = content[end_index + len(marker) :]
    try:
        parsed = yaml.safe_load(raw_frontmatter) or {}
    except yaml.YAMLError:
        logger.debug("Failed to parse frontmatter from %s", path, exc_info=True)
        parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}
    return parsed, body.strip()


def _extract_description(frontmatter: dict[str, Any], body: str, *, fallback: str) -> str:
    description = frontmatter.get("description")
    if isinstance(description, str) and description.strip():
        return description.strip()
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            stripped = stripped.lstrip("#").strip()
        if stripped:
            return stripped
    return fallback


def _walk_plugin_markdown(
    root: Path,
    *,
    stop_at_skill_dir: bool,
) -> list[Path]:
    if not root.exists():
        return []
    files: list[Path] = []
    for current_root, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(current_root)
        dirnames[:] = [d for d in dirnames if not (current / d).is_symlink()]
        skill_file = current / "SKILL.md"
        if (
            stop_at_skill_dir
            and skill_file.exists()
            and skill_file.resolve().is_relative_to(root.resolve())
        ):
            files.append(skill_file)
            dirnames[:] = []
            continue
        for filename in sorted(filenames):
            if filename.lower().endswith(".md") and (current / filename).resolve().is_relative_to(
                root.resolve()
            ):
                files.append(current / filename)
    return sorted(files)


def _load_plugin_skills(path: Path) -> list[SkillDefinition]:
    """Load plugin entrypoints without recursing into references or templates."""
    return load_skills_from_dirs([path], source="plugin", create_missing=False)


def _load_plugin_hooks(path: Path) -> dict[str, list[HookDefinition]]:
    """Load hooks from a flat hooks.json file."""
    if not path.exists():
        return {}
    from openharness.hooks.schemas import (
        AgentHookDefinition,
        CommandHookDefinition,
        HttpHookDefinition,
        PromptHookDefinition,
    )

    raw = json.loads(path.read_text(encoding="utf-8"))
    parsed: dict[str, list[HookDefinition]] = {}
    for event, hooks in raw.items():
        parsed[event] = []
        for hook in hooks:
            hook_type = hook.get("type")
            if hook_type == "command":
                parsed[event].append(CommandHookDefinition.model_validate(hook))
            elif hook_type == "prompt":
                parsed[event].append(PromptHookDefinition.model_validate(hook))
            elif hook_type == "http":
                parsed[event].append(HttpHookDefinition.model_validate(hook))
            elif hook_type == "agent":
                parsed[event].append(AgentHookDefinition.model_validate(hook))
    return parsed


def _load_plugin_hooks_structured(path: Path, plugin_root: Path) -> dict[str, list[HookDefinition]]:
    """Load hooks from structured hooks.json format."""
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    hooks_data = raw.get("hooks", raw)
    if not isinstance(hooks_data, dict):
        return {}
    parsed: dict[str, list[HookDefinition]] = {}
    for event, entries in hooks_data.items():
        if not isinstance(entries, list):
            continue
        parsed[event] = []
        for entry in entries:
            hook_list = entry.get("hooks", [])
            matcher = entry.get("matcher", "")
            for hook in hook_list:
                cmd = hook.get("command", "")
                cmd = cmd.replace("${CLAUDE_PLUGIN_ROOT}", str(plugin_root))
                parsed[event].append(
                    TypeAdapter(HookDefinition).validate_python(
                        {
                            "type": hook.get("type", "command"),
                            "command": cmd,
                            "matcher": matcher,
                            "timeout_seconds": hook.get("timeout") or 30,
                        }
                    )
                )
    return parsed


def _load_plugin_mcp(path: Path) -> dict[str, McpServerConfig]:
    """Load MCP server configuration from a JSON file."""
    if not path.exists():
        return {}
    from openharness.mcp.types import McpJsonConfig

    raw = json.loads(path.read_text(encoding="utf-8"))
    parsed = McpJsonConfig.model_validate(raw)
    return parsed.mcpServers


def _load_plugin_tools(path: Path, manifest: PluginManifest) -> list[BaseTool[BaseModel]]:
    """Discover and instantiate BaseTool subclasses from a plugin's tools/ directory."""
    from openharness.tools.base import BaseTool

    tools_dir = path / manifest.tools_dir
    if not tools_dir.is_dir():
        return []

    tools: list[BaseTool[BaseModel]] = []
    for py_file in sorted(tools_dir.glob("*.py")):
        if not py_file.resolve().is_relative_to(path.resolve()):
            continue
        if py_file.name.startswith("_"):
            continue
        module_name = f"_plugin_tools_{manifest.name}_{py_file.stem}"
        try:
            spec = importlib.util.spec_from_file_location(module_name, py_file)
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
        except Exception:
            logger.debug("Failed to load plugin tool module %s", py_file, exc_info=True)
            continue

        for attr_name in dir(module):
            attr = getattr(module, attr_name, None)
            if (
                isinstance(attr, type)
                and issubclass(attr, BaseTool)
                and attr is not BaseTool
                and hasattr(attr, "name")
                and hasattr(attr, "description")
            ):
                try:
                    instance = attr()
                    tools.append(instance)
                    logger.debug("Loaded plugin tool: %s from %s", instance.name, py_file)
                except Exception:
                    logger.debug(
                        "Failed to instantiate tool %s from %s", attr_name, py_file, exc_info=True
                    )
    return tools
