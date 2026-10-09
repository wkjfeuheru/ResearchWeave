"""Contracts for the retained product, configuration migration and retired imports."""

import ast
import json
from pathlib import Path

import pytest

from openharness.config.settings import Settings, save_settings
from openharness.tools import create_research_tool_registry
from openharness.utils.redaction import _redact_config_value, _settings_json_for_display


RETIRED = {
    "autopilot",
    "bridge",
    "channels",
    "commands",
    "coordinator",
    "keybindings",
    "memory",
    "output_styles",
    "personalization",
    "state",
    "swarm",
    "tasks",
    "themes",
    "ui",
    "vim",
    "voice",
    "worker",
}


def test_package_has_no_retired_imports_or_dynamic_loads():
    package = Path(__file__).resolve().parents[2] / "src" / "openharness"
    forbidden = {"openharness." + name for name in RETIRED} | {
        "openharness.services.cron",
        "openharness.services.cron_scheduler",
        "openharness.services.autodream",
        "openharness.services.memory_extract",
        "openharness.services.session_memory",
        "openharness.services.lsp",
        "openharness.services.session_backend",
        "openharness.prompts.claudemd",
        "openharness.config.schema",
        "ohmo",
    }
    for name in RETIRED:
        assert not (package / name).exists()
    for path in package.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        candidates = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                candidates.append(node.module)
            elif isinstance(node, ast.Import):
                candidates.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                # A module name in a dynamic import must be checked too.
                candidates.append(node.value)
        for candidate in candidates:
            assert not any(
                candidate == name or candidate.startswith(name + ".") for name in forbidden
            ), (path, candidate)


def test_registry_contains_only_research_tools():
    names = {tool.name for tool in create_research_tool_registry().list_tools()}
    assert names == {
        "ask_user_question",
        "bash",
        "edit_file",
        "read_file",
        "write_file",
        "glob",
        "grep",
        "image_to_text",
        "research_memory",
        "planner",
        "replanner",
        "research_project",
        "dispatch_subagents",
        "skill",
        "tool_search",
        "web_fetch",
        "web_search",
    }


@pytest.mark.parametrize("key", ["context_window_tokens", "auto_compact_threshold_tokens"])
def test_legacy_budget_migrates_only_without_current_budget(key):
    legacy = {"memory": {"enabled": True, key: 10000}, "theme": "old", "voice_mode": True}
    settings = Settings.model_validate(legacy)
    assert getattr(settings, key) == 10000
    assert "memory" not in settings.model_dump()
    assert "theme" not in settings.model_dump()
    assert "voice_mode" not in settings.model_dump()
    assert getattr(Settings.model_validate({**legacy, key: 20000}), key) == 20000
    profile = Settings().merged_profiles()["claude-api"].model_dump() | {key: 30000}
    current = Settings.model_validate(
        {**legacy, "active_profile": "claude-api", "profiles": {"claude-api": profile}}
    )
    assert getattr(current.materialize_active_profile(), key) == 30000


def test_config_redaction_covers_nested_and_retired_credentials():
    data = {
        "old": {"qdrant_api_key": "old-vector-key", "embedding_api_key": "old-embed-key"},
        "headers": {"Authorization": "Bearer header-key"},
        "nested": [{"password": "password-key", "safe": "description"}],
    }
    clean = json.dumps(_redact_config_value(data))
    assert "old-vector-key" not in clean and "old-embed-key" not in clean
    assert "header-key" not in clean and "password-key" not in clean
    assert "description" in clean
    settings = Settings(api_key="model-key", vision={"api_key": "vision-key"})
    assert "model-key" not in _settings_json_for_display(settings)
    assert "vision-key" not in _settings_json_for_display(settings)


@pytest.mark.asyncio
async def test_memory_disabled_ignores_retired_state_and_plugins_keep_hooks(tmp_path, monkeypatch):
    from openharness.runtime import build_runtime, close_runtime
    from openharness.hooks import HookEvent
    from tests.test_engine.test_query_engine import StaticApiClient

    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))
    save_settings(Settings(research_memory={"enabled": False}))
    plugin = tmp_path / "plugins" / "research"
    plugin.mkdir(parents=True)
    (plugin / "plugin.json").write_text(json.dumps({"name": "research", "version": "1"}))
    (plugin / "hooks.json").write_text(
        json.dumps({"session_start": [{"type": "command", "command": "printf RESEARCH_HOOK_OK"}]})
    )
    bundle = await build_runtime(
        cwd=str(tmp_path),
        session_id="a" * 12,
        api_client=StaticApiClient("ok"),
        extra_plugin_roots=[plugin.parent],
        restore_tool_metadata={
            "research_mode": False,
            "long_term_memory_store": object(),
            "task_focus_state": {"goal": "old coding goal"},
        },
    )
    try:
        assert "research_memory" not in {tool.name for tool in bundle.tool_registry.list_tools()}
        assert "investigate_conflict" not in {
            tool.name for tool in bundle.tool_registry.list_tools()
        }
        assert "research_store" not in bundle.engine.tool_metadata
        assert not (
            {"research_mode", "long_term_memory_store", "task_focus_state"}
            & bundle.engine.tool_metadata.keys()
        )
        assert not hasattr(bundle, "app_state") and not hasattr(bundle, "commands")
        result = await bundle.hook_executor.execute(HookEvent.SESSION_START, {"cwd": str(tmp_path)})
        assert any("RESEARCH_HOOK_OK" in item.output for item in result.results)
    finally:
        await close_runtime(bundle)


async def test_runtime_filters_retired_plugin_tools_but_keeps_services_and_config(
    tmp_path, monkeypatch
):
    from openharness.runtime import build_runtime, close_runtime
    from openharness.tools import RESEARCH_EXCLUDED_TOOLS
    from tests.test_engine.test_query_engine import StaticApiClient

    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))
    plugin = tmp_path / "plugins/research"
    (plugin / "tools").mkdir(parents=True)
    (plugin / "plugin.json").write_text(
        json.dumps({"name": "research", "version": "1"}), encoding="utf-8"
    )
    classes = []
    for index, name in enumerate(sorted(RESEARCH_EXCLUDED_TOOLS) + ["plugin_research_reader"]):
        classes.append(f"""class PluginTool{index}(BaseTool):
    name = {name!r}
    description = "Plugin compatibility fixture"
    input_model = PluginInput
    async def execute(self, arguments, context):
        return ToolResult(output="ok")
""")
    (plugin / "tools/fixtures.py").write_text(
        "from openharness.tools.base import BaseTool, ToolResult\nfrom pydantic import BaseModel\nclass PluginInput(BaseModel):\n    pass\n"
        + "\n".join(classes),
        encoding="utf-8",
    )
    bundle = await build_runtime(
        cwd=str(tmp_path),
        session_id="b" * 12,
        api_client=StaticApiClient("ok"),
        extra_plugin_roots=[plugin.parent],
        connect_mcp=False,
        settings_override=Settings(
            research_memory={
                "subagent_max_concurrency": 2,
                "subagent_max_calls": 4,
                "subagent_timeout_seconds": 20,
            }
        ),
    )
    try:
        names = {tool.name for tool in bundle.tool_registry.list_tools()}
        assert RESEARCH_EXCLUDED_TOOLS.isdisjoint(names)
        assert {
            "plugin_research_reader",
            "dispatch_subagents",
            "list_mcp_resources",
            "read_mcp_resource",
        } <= names
        assert bundle.mcp_manager is not None
        assert bundle.engine.tool_metadata["subagent_max_concurrency"] == 2
        assert bundle.engine.tool_metadata["subagent_max_calls"] == 4
        assert bundle.engine.tool_metadata["subagent_timeout_seconds"] == 20
    finally:
        await close_runtime(bundle)
