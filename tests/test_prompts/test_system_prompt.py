"""Tests for researchx.prompts.system_prompt."""

from __future__ import annotations

from researchx.prompts.environment import EnvironmentInfo
from researchx.prompts.system_prompt import build_system_prompt


def _make_env(**overrides) -> EnvironmentInfo:
    defaults = dict(
        os_name="Linux",
        os_version="5.15.0",
        platform_machine="x86_64",
        shell="bash",
        cwd="/home/user/project",
        home_dir="/home/user",
        date="2026-04-01",
        python_version="3.10.17",
        python_executable="/home/user/.researchx-venv/bin/python",
        virtual_env="/home/user/.researchx-venv",
        hostname="testhost",
    )
    defaults.update(overrides)
    return EnvironmentInfo(**defaults)


def test_build_system_prompt_contains_environment():
    env = _make_env()
    prompt = build_system_prompt(env=env)
    assert "Linux 5.15.0" in prompt
    assert "x86_64" in prompt
    assert "bash" in prompt
    assert "/home/user/project" in prompt
    assert "2026-04-01" in prompt
    assert "3.10.17" in prompt
    assert "/home/user/.researchx-venv/bin/python" in prompt
    assert "虚拟环境：/home/user/.researchx-venv" in prompt


def test_build_system_prompt_no_git():
    env = _make_env()
    prompt = build_system_prompt(env=env)
    assert "Git:" not in prompt


def test_build_system_prompt_custom_prompt():
    env = _make_env()
    prompt = build_system_prompt(custom_prompt="You are a helpful bot.", env=env)
    assert prompt.startswith("You are a helpful bot.")
    assert "Linux 5.15.0" in prompt
    # Base prompt should not appear
    assert "ResearchX" not in prompt


def test_build_system_prompt_default_includes_base():
    env = _make_env()
    prompt = build_system_prompt(env=env)
    assert "ResearchX" in prompt
