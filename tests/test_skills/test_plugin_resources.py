"""Packaged plugin lifecycle and progressive skill resource loading."""

import json
import shutil
from pathlib import Path

import pytest

from openharness.config.settings import Settings
from openharness.plugins.loader import BUNDLED_PLUGINS_DIR, load_plugins
from openharness.skills import load_skill_registry
from openharness.tools.base import ToolExecutionContext
from openharness.tools.file_read_tool import FileReadTool, FileReadToolInput
from openharness.tools.skill_tool import SkillTool, SkillToolInput


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    return tmp_path


def test_packaged_skill_is_only_an_enabled_plugin_contribution(isolated_home):
    registry = load_skill_registry(isolated_home, settings=Settings())
    creator = registry.get("skill-creator")
    assert creator is not None and creator.source == "plugin"
    assert Path(creator.path).is_relative_to(BUNDLED_PLUGINS_DIR)
    assert registry.get("replace-with-skill-name") is None
    assert not any(skill.source == "bundled" for skill in registry.list_skills())
    disabled = Settings(enabled_plugins={"skill-authoring": False})
    assert load_skill_registry(isolated_home, settings=disabled).get("skill-creator") is None
    card = next(plugin for plugin in load_plugins(disabled, isolated_home) if plugin.name == "skill-authoring")
    assert not card.enabled and len(card.skills) == 1


@pytest.mark.asyncio
async def test_packaged_plugin_resources_are_read_on_demand_from_another_cwd(isolated_home):
    work = isolated_home / "work"
    work.mkdir()
    context = ToolExecutionContext(cwd=work, metadata={"skill_settings": Settings()})
    result = await SkillTool().execute(SkillToolInput(name="skill-creator"), context)
    assert not result.is_error
    base = Path(result.metadata["skill_base_dir"])
    assert base.is_absolute() and str(base / "templates" / "skill" / "SKILL.md") in result.output
    path = base / "references" / "standards.md"
    assert str(path) in result.output
    # Only the entrypoint is loaded, not the body of its resource files.
    assert "研究资料与执行指令分开" not in result.output
    loaded = await FileReadTool().execute(FileReadToolInput(path=str(path)), context)
    assert not loaded.is_error and "研究资料与执行指令分开" in loaded.output


@pytest.mark.asyncio
async def test_disabled_packaged_plugin_cannot_be_loaded_by_tool(isolated_home):
    result = await SkillTool().execute(
        SkillToolInput(name="skill-creator"),
        ToolExecutionContext(cwd=isolated_home, metadata={
            "skill_settings": Settings(enabled_plugins={"skill-authoring": False}),
        }),
    )
    assert result.is_error and "Skill not found" in result.output


def test_directory_skill_wins_over_legacy_and_nested_templates_are_not_discovered(isolated_home):
    root = isolated_home / "extra"
    skill_root = root / "fixture" / "skills"
    (skill_root / "report" / "templates" / "nested").mkdir(parents=True)
    (root / "fixture" / "plugin.json").write_text(json.dumps({"name": "fixture"}))
    (skill_root / "report.md").write_text("---\nname: report\ndescription: Legacy\n---\nOld body")
    (skill_root / "legacy.md").write_text("---\nname: legacy\ndescription: Legacy supported\n---\n")
    entrypoint = skill_root / "report" / "SKILL.md"
    entrypoint.write_text("---\nname: report\ndescription: >\n  Folded plugin\n  description.\n---\nNew body")
    (skill_root / "report" / "templates" / "nested" / "SKILL.md").write_text(
        "---\nname: not-a-skill\ndescription: Template\n---\n",
    )
    registry = load_skill_registry(isolated_home, settings=Settings(), extra_plugin_roots=[root])
    assert registry.get("report").path == str(entrypoint)
    assert registry.get("report").description == "Folded plugin description."
    assert registry.get("legacy") is not None
    assert registry.get("not-a-skill") is None


def test_user_plugin_can_replace_packaged_plugin_without_duplicate_cards(isolated_home):
    root = isolated_home / "config" / "plugins" / "custom-authoring"
    (root / "skills" / "custom").mkdir(parents=True)
    (root / "plugin.json").write_text(json.dumps({"name": "skill-authoring"}))
    (root / "skills" / "custom" / "SKILL.md").write_text("---\nname: custom\ndescription: Custom\n---\n")
    plugins = load_plugins(Settings(), isolated_home)
    matches = [plugin for plugin in plugins if plugin.name == "skill-authoring"]
    assert len(matches) == 1 and matches[0].path == root
    registry = load_skill_registry(isolated_home, settings=Settings())
    assert registry.get("custom") is not None and registry.get("skill-creator") is None


def test_compatible_user_skill_can_override_packaged_default(isolated_home):
    root = isolated_home / "config" / "skills" / "skill-creator"
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text("---\nname: skill-creator\ndescription: Custom creator\n---\n")
    creator = load_skill_registry(isolated_home, settings=Settings()).get("skill-creator")
    assert creator.source == "user" and creator.path == str(root / "SKILL.md")


@pytest.mark.asyncio
async def test_listing_resources_neither_executes_scripts_nor_reads_outside_symlinks(isolated_home):
    root = isolated_home / "extra"
    base = root / "fixture" / "skills" / "fixture"
    (base / "scripts").mkdir(parents=True)
    (base / "references").mkdir()
    (root / "fixture" / "plugin.json").write_text(json.dumps({"name": "fixture"}))
    (base / "SKILL.md").write_text("---\nname: fixture\ndescription: Fixture\n---\nEntry only")
    marker = isolated_home / "executed"
    (base / "scripts" / "transform.py").write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
    outside = isolated_home / "outside.md"
    outside.write_text("OUTSIDE RESOURCE")
    (base / "references" / "outside.md").symlink_to(outside)
    result = await SkillTool().execute(SkillToolInput(name="fixture"), ToolExecutionContext(
        cwd=isolated_home, metadata={"extra_plugin_roots": [root], "skill_settings": Settings()},
    ))
    assert not result.is_error and "transform.py" in result.output
    assert "OUTSIDE RESOURCE" not in result.output and str(outside) not in result.output
    assert not marker.exists()


def test_full_research_plugins_have_metadata_and_independent_switches(isolated_home):
    names = {"financial-statement-analysis", "company-event-monitor", "research-report-digest", "deep-investment-report"}
    enabled = load_skill_registry(isolated_home, settings=Settings())
    for name in names:
        skill = enabled.get(name)
        assert skill and skill.metadata.status == "active"
        assert skill.metadata.skill_id == name and len(skill.metadata.content_hash) == 64
        assert {"bash", "read_file", "research_memory"} <= set(skill.metadata.required_tools)
        registry = load_skill_registry(isolated_home, settings=Settings(enabled_plugins={name: False}))
        assert registry.get(name) is None
        assert all(registry.get(other) is not None for other in names - {name})
    disabled = load_skill_registry(isolated_home, settings=Settings(enabled_plugins={"financial-statement-analysis": False}))
    assert disabled.get("deep-investment-report") and not disabled.get("financial-statement-analysis")
    assert "不修改启用配置" in disabled.get("deep-investment-report").content


def test_metadata_hash_stable_resources_change_and_hash_field_excluded(isolated_home):
    from openharness.skills.metadata import content_hash
    base = isolated_home / "example"
    (base / "references").mkdir(parents=True)
    entry = base / "SKILL.md"
    entry.write_text("---\nname: example\ncontent_hash: abc\n---\nBody")
    resource = base / "references" / "standard.md"
    resource.write_text("Rules")
    first = content_hash(entry)
    assert content_hash(entry) == first
    entry.write_text(entry.read_text().replace("abc", "different"))
    assert content_hash(entry) == first
    (base / "references" / "__pycache__").mkdir()
    (base / "references" / "__pycache__" / "ignore.pyc").write_bytes(b"cache")
    assert content_hash(entry) == first
    resource.write_text("New rules")
    assert content_hash(entry) != first


def test_skill_hash_covers_its_relocated_business_implementation(isolated_home):
    from openharness.skills.metadata import content_hash

    source = BUNDLED_PLUGINS_DIR / "financial-statement-analysis" / "skills" / "financial-statement-analysis"
    base = isolated_home / "financial-skill-copy"
    shutil.copytree(source, base, ignore=shutil.ignore_patterns("__pycache__"))
    entry = base / "SKILL.md"
    first = content_hash(entry)
    script = base / "scripts" / "analyze_statements.py"
    script.write_text(script.read_text().replace("denominator == 0", "denominator <= 0"))
    assert content_hash(entry) != first


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["draft", "retired"])
async def test_lifecycle_prevents_auto_invocation_and_skill_execution(isolated_home, status):
    from openharness.prompts.context import _build_skills_section
    from openharness.skills.loader import load_skills_from_dirs
    base = isolated_home / "fixture"
    base.mkdir()
    (base / "SKILL.md").write_text(f"---\nname: fixture\ndescription: Fixture\nstatus: {status}\n---\nBody")
    assert load_skills_from_dirs([base])[0].metadata.status == status
    assert "**fixture**" not in (_build_skills_section(isolated_home, extra_skill_dirs=[base], settings=Settings()) or "")
    context = ToolExecutionContext(cwd=isolated_home, metadata={"extra_skill_dirs": [base], "skill_settings": Settings()})
    result = await SkillTool().execute(SkillToolInput(name="fixture"), context)
    assert result.is_error and status in result.output
