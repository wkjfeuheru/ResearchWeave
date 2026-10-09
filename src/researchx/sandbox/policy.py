"""Host-derived report execution policy; model arguments never define permissions."""

from __future__ import annotations

import os
import sys
import shutil
from dataclasses import dataclass
from pathlib import Path

from researchx.config import Settings


@dataclass(frozen=True)
class ExecutionOwner:
    runtime_id: str
    project_id: str
    execution_id: str
    workspace: Path

    @property
    def key(self) -> str:
        return f"{self.runtime_id}:{self.project_id}:{self.execution_id}"


def report_settings(settings: Settings, workspace: Path) -> Settings:
    # A pre-existing hardlink keeps the host inode even behind a bind mount.
    # Apply the same no-hardlinks boundary as Runtime's existing file tools.
    from researchx.sandbox.adapter import SandboxUnavailableError

    for path in workspace.rglob("*"):
        if not path.is_symlink() and path.is_file() and path.stat().st_nlink > 1:
            raise SandboxUnavailableError(f"Report workspace contains an unsafe hardlink: {path}")
    derived = settings.model_copy(deep=True)
    sandbox = derived.sandbox
    sandbox.enabled = True
    sandbox.required_srt_version = "0.0.79"
    sandbox.fail_if_unavailable = True
    sandbox.enabled_platforms = []
    sandbox.network.allowed_domains = []
    sandbox.network.denied_domains = []
    dependencies = [Path(path) for path in ("/usr", "/bin", "/sbin", "/lib", "/lib64")]
    srt = shutil.which("srt")
    if srt:
        dependencies.append(Path(srt).resolve().parents[1])
    dependencies += [Path(binary) for name in ("socat", "rg") if (binary := shutil.which(name))]
    dependencies += [
        Path(sys.base_prefix),
        Path(sys.prefix),
        Path(__file__).resolve().parents[1],
        Path("/etc/ld.so.cache"),
        Path("/etc/localtime"),
    ]
    executable = Path(sys.executable)
    if executable.is_symlink():
        dependencies.append(executable.readlink().parent.parent)
    sandbox.filesystem.deny_read = ["/"]
    sandbox.filesystem.allow_read = [str(path) for path in dependencies if path.exists()] + [
        str(path)
        for path in workspace.iterdir()
        if path.name not in {"artifacts", "reports", "subagents"} and not path.is_symlink()
    ]
    sandbox.filesystem.allow_write = [
        str(workspace / path) for path in ("artifacts", "reports", "subagents")
    ]
    sandbox.filesystem.deny_write = [str(workspace / "MEMORY.md")]
    sandbox.docker.extra_mounts = []
    sandbox.docker.extra_env = {}
    return derived


def report_environment(workspace: Path) -> dict[str, str]:
    # A whitelist also excludes loader/Python injection variables, proxy credentials,
    # cloud keys and RESEARCHX_RESEARCH_SESSION_DIR. No host HOME is inherited.
    return {
        "PATH": os.pathsep.join(
            (
                str(Path(sys.executable).parent),
                str(Path(shutil.which("socat") or "/usr/bin/socat").parent),
                str(Path(shutil.which("rg") or "/usr/bin/rg").parent),
                "/usr/local/bin",
                "/usr/bin",
                "/bin",
            )
        ),
        "HOME": str(workspace),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "RESEARCHX_ISOLATED_EXPORT": "1",
    }


def agent_shell_settings(settings: Settings, workspace: Path) -> Settings:
    """Reuse the strict report backend; ordinary commands may write within cwd."""
    derived = report_settings(settings, workspace)
    derived.sandbox.filesystem.allow_read.append(str(workspace.resolve()))
    derived.sandbox.filesystem.allow_write = [str(workspace.resolve())]
    from researchx.config.paths import get_config_dir, get_data_dir

    private = [str(get_config_dir().resolve()), str(get_data_dir().resolve())]
    derived.sandbox.filesystem.deny_read = ["/", *private]
    derived.sandbox.filesystem.deny_write.extend(private)
    return derived
