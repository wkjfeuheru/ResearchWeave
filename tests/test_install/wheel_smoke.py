"""Opt-in installed-wheel smoke; run with the clean environment's Python."""

import asyncio
import importlib
import pkgutil
import tempfile
import os
from importlib.metadata import distribution
from pathlib import Path
import httpx
from openharness.tools import create_research_tool_registry
from openharness.tools.base import ToolExecutionContext
from openharness.tools.bash_tool import BashTool, BashToolInput
from openharness.research.store import ResearchStore
from openharness.skills import load_skill_registry
from openharness.web.app import create_app


async def main():
    import openharness

    modules = list(pkgutil.walk_packages(openharness.__path__, "openharness."))
    for module in modules:
        importlib.import_module(module.name)
    dist = distribution("openharness-ai")
    names = [str(f) for f in dist.files]
    assert not any(
        "/ui/" in f or "/channels/" in f or "ohmo/" in f or "/_frontend/" in f or "/memory/" in f
        for f in names
    )
    assert any("/_web/index.html" in f for f in names)
    plugin_prefix = "openharness/plugins/bundled/skill-authoring/"
    assert plugin_prefix + "plugin.json" in names
    assert plugin_prefix + "skills/skill-creator/templates/skill/SKILL.md" in names
    assert plugin_prefix + "skills/skill-creator/templates/skill/scripts/.gitkeep" in names
    research_plugins = {"financial-statement-analysis", "company-event-monitor", "deep-investment-report", "research-report-digest"}
    for plugin in research_plugins:
        base = f"openharness/plugins/bundled/{plugin}/"
        assert base + "plugin.json" in names
        for resource in ("SKILL.md", "scripts/run.py", "templates/input.schema.json", "templates/report.md"):
            assert base + f"skills/{plugin}/" + resource in names
    assert not any("sample_plugins" in f for f in names)
    assert not any(f.startswith("openharness/skills/bundled/") for f in names)
    assert {e.name for e in dist.entry_points} == {"oh", "openh", "openharness"}
    with tempfile.TemporaryDirectory() as tmp:
        cwd = Path(tmp)
        os.environ["OPENHARNESS_CONFIG_DIR"] = str(cwd / "config")
        os.environ["OPENHARNESS_DATA_DIR"] = str(cwd / "data")
        creator = load_skill_registry(cwd).get("skill-creator")
        assert creator is not None and creator.source == "plugin"
        assert (Path(creator.base_dir) / "templates" / "skill" / "SKILL.md").is_file()
        for plugin in research_plugins:
            skill = load_skill_registry(cwd).get(plugin)
            assert skill is not None and skill.metadata.status == "active"
            assert len(skill.metadata.content_hash) == 64
        store = ResearchStore(cwd, "a" * 12)
        source = store.capture(origin_id="user", kind="user", content="wheel smoke")
        assert store.read_source(source) == "wheel smoke"
        assert "research_memory" in {t.name for t in create_research_tool_registry().list_tools()}
        result = await BashTool().execute(
            BashToolInput(command="printf WHEEL_SHELL_OK"), ToolExecutionContext(cwd=cwd)
        )
        assert not result.is_error and result.output == "WHEEL_SHELL_OK"
        app = create_app(cwd=cwd)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as client:
            assert (await client.get("/api/health")).status_code == 200
            assert (await client.get("/")).status_code == 200
            item = await client.post(
                "/api/sessions",
                json={"profile_id": "claude-api"},
                headers={"origin": "http://localhost"},
            )
            assert item.status_code == 201, item.text
    print(
        f"PASS: {len(modules)} installed modules imported; no retired assets; Web/CLI/Shell/research persistence verified"
    )


if __name__ == "__main__":
    asyncio.run(main())
