"""Tests for researchx.prompts.environment."""

from __future__ import annotations

from pathlib import Path

from researchx.prompts.environment import (
    EnvironmentInfo,
    detect_os,
    detect_shell,
    get_environment_info,
)


def test_detect_os_returns_tuple():
    os_name, os_version = detect_os()
    assert isinstance(os_name, str)
    assert isinstance(os_version, str)
    assert len(os_name) > 0


def test_detect_shell_returns_string(monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/bash")
    assert detect_shell() == "bash"


def test_detect_shell_zsh(monkeypatch):
    monkeypatch.setenv("SHELL", "/usr/bin/zsh")
    assert detect_shell() == "zsh"


def test_detect_shell_fallback(monkeypatch):
    monkeypatch.delenv("SHELL", raising=False)
    shell = detect_shell()
    # Should find something on PATH or return "unknown"
    assert isinstance(shell, str)


def test_get_environment_info_returns_dataclass():
    info = get_environment_info()
    assert isinstance(info, EnvironmentInfo)
    assert len(info.os_name) > 0
    assert len(info.shell) > 0
    assert len(info.cwd) > 0
    assert len(info.date) == 10  # YYYY-MM-DD
    assert len(info.python_version) > 0
    assert len(info.python_executable) > 0


def test_get_environment_info_detects_virtual_env_from_python_executable(
    monkeypatch, tmp_path: Path
):
    venv_root = tmp_path / ".researchx-venv"
    bin_dir = venv_root / "bin"
    bin_dir.mkdir(parents=True)
    (venv_root / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
    fake_python = bin_dir / "python"
    fake_python.write_text("", encoding="utf-8")

    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.setattr("researchx.prompts.environment.sys.executable", str(fake_python))

    info = get_environment_info(cwd=str(tmp_path))

    assert info.python_executable == str(fake_python.resolve())
    assert info.virtual_env == str(venv_root.resolve())


def test_get_environment_info_cwd_override(tmp_path: Path):
    info = get_environment_info(cwd=str(tmp_path))
    assert info.cwd == str(tmp_path)
