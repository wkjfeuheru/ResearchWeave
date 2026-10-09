"""Environment detection for system prompt construction.

Gathers OS, shell, platform, working directory, date, and git info.
"""

from __future__ import annotations

import os
import platform
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class EnvironmentInfo:
    """Snapshot of the current runtime environment."""

    os_name: str
    os_version: str
    platform_machine: str
    shell: str
    cwd: str
    home_dir: str
    date: str
    python_version: str
    python_executable: str
    virtual_env: str | None
    hostname: str = ""
    extra: dict[str, str] = field(default_factory=dict)


def detect_os() -> tuple[str, str]:
    """Return (os_name, os_version) for the current platform."""
    system = platform.system()
    if system == "Linux":
        try:
            import distro

            return "Linux", distro.version(pretty=True) or platform.release()
        except ImportError:
            return "Linux", platform.release()
    elif system == "Darwin":
        mac_ver = platform.mac_ver()[0]
        return "macOS", mac_ver or platform.release()
    elif system == "Windows":
        win_ver = platform.version()
        return "Windows", win_ver
    return system, platform.release()


def detect_shell() -> str:
    """Detect the user's shell."""
    shell = os.environ.get("SHELL", "")
    if shell:
        return Path(shell).name

    # Fallback: check for common shells on PATH
    for candidate in ("bash", "zsh", "fish", "sh"):
        if shutil.which(candidate):
            return candidate

    return "unknown"


def get_environment_info(cwd: str | None = None) -> EnvironmentInfo:
    """Gather all environment information into an EnvironmentInfo snapshot."""
    if cwd is None:
        cwd = os.getcwd()

    python_executable = str(Path(sys.executable).resolve())
    virtual_env = os.environ.get("VIRTUAL_ENV")
    if not virtual_env:
        executable_path = Path(python_executable)
        candidate = executable_path.parent.parent
        if (
            executable_path.parent.name in {"bin", "Scripts"}
            and (candidate / "pyvenv.cfg").exists()
        ):
            virtual_env = str(candidate)

    os_name, os_version = detect_os()
    shell = detect_shell()

    return EnvironmentInfo(
        os_name=os_name,
        os_version=os_version,
        platform_machine=platform.machine(),
        shell=shell,
        cwd=cwd,
        home_dir=str(Path.home()),
        date=datetime.now(tz=timezone.utc).strftime("%Y-%m-%d"),
        python_version=platform.python_version(),
        python_executable=python_executable,
        virtual_env=virtual_env,
        hostname=platform.node(),
    )
