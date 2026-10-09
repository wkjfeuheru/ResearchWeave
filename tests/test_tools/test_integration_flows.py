"""Higher-level integration flows across multiple built-in tools."""

from __future__ import annotations

from pathlib import Path

import pytest

from openharness.tools import create_research_tool_registry
from openharness.tools.base import ToolExecutionContext


@pytest.mark.asyncio
async def test_search_edit_flow_across_registry(tmp_path: Path):
    registry = create_research_tool_registry()
    context = ToolExecutionContext(cwd=tmp_path, metadata={"tool_registry": registry})

    write = registry.get("write_file")
    glob = registry.get("glob")
    grep = registry.get("grep")
    edit = registry.get("edit_file")
    read = registry.get("read_file")

    await write.execute(
        write.input_model(path="src/demo.py", content="alpha\nbeta\n"),
        context,
    )
    glob_result = await glob.execute(glob.input_model(pattern="**/*.py"), context)
    assert "src/demo.py" in glob_result.output.replace("\\", "/")

    grep_result = await grep.execute(
        grep.input_model(pattern="beta", file_glob="**/*.py"),
        context,
    )
    assert "src/demo.py:2:beta" in grep_result.output.replace("\\", "/")

    await edit.execute(
        edit.input_model(path="src/demo.py", old_str="beta", new_str="gamma"),
        context,
    )
    read_result = await read.execute(read.input_model(path="src/demo.py"), context)
    assert "gamma" in read_result.output
    assert "beta" not in (tmp_path / "src" / "demo.py").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_skill_and_config_flow_across_registry(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    skills_dir = tmp_path / "config" / "skills"
    skills_dir.mkdir(parents=True)
    pytest_dir = skills_dir / "pytest"
    pytest_dir.mkdir()
    (pytest_dir / "SKILL.md").write_text(
        "# Pytest\nPytest fixtures help reuse setup.\n",
        encoding="utf-8",
    )

    registry = create_research_tool_registry()
    context = ToolExecutionContext(cwd=tmp_path, metadata={"tool_registry": registry})

    registry = create_research_tool_registry(mode="general")
    context.metadata["tool_registry"] = registry
    config = registry.get("config")
    skill = registry.get("skill")

    set_result = await config.execute(
        config.input_model(action="set", key="effort", value="high"),
        context,
    )
    assert set_result.output == "Updated effort"

    show_result = await config.execute(config.input_model(action="show"), context)
    assert '"effort": "high"' in show_result.output

    skill_result = await skill.execute(skill.input_model(name="Pytest"), context)
    assert "fixtures" in skill_result.output


@pytest.mark.asyncio
async def test_ask_user_question_flow_across_registry(tmp_path: Path):
    registry = create_research_tool_registry()

    async def _answer(question: str) -> str:
        assert "favorite color" in question
        return "green"

    context = ToolExecutionContext(
        cwd=tmp_path,
        metadata={"tool_registry": registry, "ask_user_prompt": _answer},
    )
    ask_user = registry.get("ask_user_question")
    write = registry.get("write_file")
    read = registry.get("read_file")

    answer_result = await ask_user.execute(
        ask_user.input_model(question="What is your favorite color?"),
        context,
    )
    assert answer_result.output == "green"

    await write.execute(
        write.input_model(path="answer.txt", content=answer_result.output),
        context,
    )
    read_result = await read.execute(read.input_model(path="answer.txt"), context)
    assert "green" in read_result.output


@pytest.mark.asyncio
async def test_notebook_flow_across_registry(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))
    registry = create_research_tool_registry()
    context = ToolExecutionContext(cwd=tmp_path, metadata={"tool_registry": registry})

    registry = create_research_tool_registry(mode="general")
    context.metadata["tool_registry"] = registry
    notebook = registry.get("notebook_edit")
    notebook_result = await notebook.execute(
        notebook.input_model(path="nb/demo.ipynb", cell_index=0, new_source="print('flow ok')\n"),
        context,
    )
    assert notebook_result.is_error is False
    assert "flow ok" in (tmp_path / "nb" / "demo.ipynb").read_text(encoding="utf-8")
