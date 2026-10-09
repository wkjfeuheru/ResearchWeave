"""Opt-in installed-wheel smoke; run with the clean environment's Python."""

import asyncio
import importlib
import json
import pkgutil
import tempfile
import os
import subprocess
import sys
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
    assert not any("skill-authoring/" in name or "skill-creator/" in name for name in names)
    research_plugins = {
        "financial-statement-analysis": ("financial", "analyze_statements"),
        "company-event-monitor": ("monitor", "normalize_events"),
        "deep-investment-report": ("deep", "forecast"),
        "research-report-digest": ("digest", "digest_reports"),
    }
    for plugin, (_, script) in research_plugins.items():
        package = "report-generation" if plugin == "deep-investment-report" else "analysis-modeling"
        base = f"openharness/plugins/bundled/{package}/"
        assert base + "plugin.json" in names
        for resource in (
            "SKILL.md",
            f"scripts/{script}.py",
            "scripts/models.py",
            "scripts/export_report.py",
            "scripts/__init__.py",
            "templates/input.schema.json",
            "templates/report.md",
        ):
            assert base + f"skills/{plugin}/" + resource in names
        assert base + f"skills/{plugin}/scripts/run.py" not in names
    assert not any("/utils/research_workflows/" in name for name in names)
    assert not any("sample_plugins" in f for f in names)
    assert not any(f.startswith("openharness/skills/bundled/") for f in names)
    assert {e.name for e in dist.entry_points} == {"oh", "openh", "openharness"}
    with tempfile.TemporaryDirectory() as tmp:
        cwd = Path(tmp)
        os.environ["OPENHARNESS_CONFIG_DIR"] = str(cwd / "config")
        os.environ["OPENHARNESS_DATA_DIR"] = str(cwd / "data")
        assert load_skill_registry(cwd).get("skill-creator") is None
        fixtures = Path(__file__).parents[1] / "fixtures" / "research_skills"
        for plugin, (kind, script) in research_plugins.items():
            skill = load_skill_registry(cwd).get(plugin)
            assert skill is not None and skill.metadata.status == "active"
            assert len(skill.metadata.content_hash) == 64, (
                "wheel build index should supply a cold L0 hash"
            )
            from openharness.skills.metadata import content_hash

            assert len(content_hash(Path(skill.path))) == 64
            assert len(load_skill_registry(cwd).get(plugin).metadata.content_hash) == 64
            scripts = Path(skill.base_dir) / "scripts"
            computed = cwd / kind / "computed.json"
            commands = [
                [sys.executable, str(scripts / f"{script}.py"), "--schema"],
                [
                    sys.executable,
                    str(scripts / f"{script}.py"),
                    "--input",
                    str(fixtures / f"{kind}.json"),
                    "--output",
                    str(computed),
                ],
                [
                    sys.executable,
                    str(scripts / "export_report.py"),
                    "--input",
                    str(computed),
                    "--output-dir",
                    str(cwd / kind),
                    "--session-dir",
                    str(cwd / "skill-session"),
                    "--task-id",
                    "installed-script",
                ],
            ]
            for command in commands:
                executed = subprocess.run(
                    command, cwd=cwd, capture_output=True, text=True, timeout=30
                )
                assert executed.returncode == 0, executed.stderr
                response = json.loads(executed.stdout)
            assert response["status"] == "complete" and len(response["artifacts"]) == 4
        for name in (
            "earnings-forecast",
            "financial-commentary",
            "industry-commentary",
            "industry-deep-dive",
        ):
            skill = load_skill_registry(cwd).get(name)
            assert skill and skill.content is None and len(skill.metadata.content_hash) == 64
            base = Path(skill.base_dir)
            script = "forecast" if name == "earnings-forecast" else "validate_report"
            input_path = (
                fixtures / "deep.json"
                if name == "earnings-forecast"
                else base / "templates/input.json"
            )
            output = cwd / name / "computed.json"
            for command in (
                [sys.executable, str(base / f"scripts/{script}.py"), "--schema"],
                [
                    sys.executable,
                    str(base / f"scripts/{script}.py"),
                    "--input",
                    str(input_path),
                    "--output",
                    str(output),
                ],
                [
                    sys.executable,
                    str(base / "scripts/export_report.py"),
                    "--input",
                    str(output),
                    "--output-dir",
                    str(cwd / name),
                ],
            ):
                executed = subprocess.run(
                    command, cwd=cwd, capture_output=True, text=True, timeout=30
                )
                assert executed.returncode == 0, executed.stderr
        parsed = subprocess.run(
            [
                sys.executable,
                "-m",
                "openharness.utils.research_documents",
                "--input",
                str(fixtures / "annual-report.pdf"),
                "--output-dir",
                str(cwd / "parsed"),
            ],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert parsed.returncode == 0, parsed.stderr
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


def test_built_wheel_includes_both_packages_and_runs_installed_smoke(tmp_path):
    """Build and execute from an isolated installation, rather than an editable checkout."""
    import zipfile

    root = Path(__file__).resolve().parents[2]
    built = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(tmp_path / "dist")],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert built.returncode == 0, built.stderr
    wheel = next((tmp_path / "dist").glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
    assert not any("skill-authoring/" in name or "skill-creator/" in name for name in names)
    for package, skills in {
        "analysis-modeling": [
            "financial-statement-analysis",
            "company-event-monitor",
            "research-report-digest",
            "earnings-forecast",
        ],
        "report-generation": [
            "deep-investment-report",
            "financial-commentary",
            "industry-commentary",
            "industry-deep-dive",
        ],
    }.items():
        base = f"openharness/plugins/bundled/{package}/"
        assert base + "plugin.json" in names
        for skill in skills:
            assert base + f"skills/{skill}/SKILL.md" in names
            assert base + f"skills/{skill}/templates/input.schema.json" in names
            assert base + f"skills/{skill}/templates/report.md" in names
    env_dir = tmp_path / "installed"
    created = subprocess.run(
        ["uv", "venv", "--python", sys.executable, str(env_dir)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert created.returncode == 0, created.stderr
    interpreter = env_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    installed = subprocess.run(
        ["uv", "pip", "install", "--python", str(interpreter), f"{wheel}[web]"],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert installed.returncode == 0, installed.stderr
    env = {
        **os.environ,
        "OPENHARNESS_CONFIG_DIR": str(tmp_path / "config"),
        "OPENHARNESS_DATA_DIR": str(tmp_path / "data"),
    }
    env.pop("PYTHONPATH", None)
    executed = subprocess.run(
        [str(interpreter), str(Path(__file__).resolve())],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert executed.returncode == 0, executed.stderr
    assert "PASS:" in executed.stdout
