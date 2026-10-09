"""Path resolution for ResearchX configuration and data directories.

Follows XDG-like conventions with ~/.researchx/ as the default base directory.
"""

from __future__ import annotations

import os
from pathlib import Path
from researchx.storage.filesystem import private_directory

_DEFAULT_BASE_DIR = ".researchx"
_LEGACY_BASE_DIR = ".openharness"
_CONFIG_FILE_NAME = "settings.json"


def migrate_legacy_base_dir() -> None:
    """Move an existing ~/.openharness installation to ~/.researchx once."""
    legacy_dir = Path.home() / _LEGACY_BASE_DIR
    current_dir = Path.home() / _DEFAULT_BASE_DIR
    if not legacy_dir.is_dir() or current_dir.exists():
        return
    try:
        legacy_dir.rename(current_dir)
    except OSError:
        pass


def get_config_dir() -> Path:
    """Return the configuration directory, creating it if needed.

    Resolution order:
    1. RESEARCHX_CONFIG_DIR environment variable
    2. ~/.researchx/
    """
    env_dir = os.environ.get("RESEARCHX_CONFIG_DIR")
    if env_dir:
        config_dir = Path(env_dir)
    else:
        migrate_legacy_base_dir()
        config_dir = Path.home() / _DEFAULT_BASE_DIR

    private_directory(config_dir)
    return config_dir


def get_config_file_path() -> Path:
    """Return the path to the main settings file (~/.researchx/settings.json)."""
    return get_config_dir() / _CONFIG_FILE_NAME


def get_data_dir() -> Path:
    """Return the data directory for caches, history, etc.

    Resolution order:
    1. RESEARCHX_DATA_DIR environment variable
    2. ~/.researchx/data/
    """
    env_dir = os.environ.get("RESEARCHX_DATA_DIR")
    if env_dir:
        data_dir = Path(env_dir)
    else:
        data_dir = get_config_dir() / "data"

    private_directory(data_dir)
    return data_dir


def get_logs_dir() -> Path:
    """Return the logs directory.

    Resolution order:
    1. RESEARCHX_LOGS_DIR environment variable
    2. ~/.researchx/logs/
    """
    env_dir = os.environ.get("RESEARCHX_LOGS_DIR")
    if env_dir:
        logs_dir = Path(env_dir)
    else:
        logs_dir = get_config_dir() / "logs"

    private_directory(logs_dir)
    return logs_dir


def get_sessions_dir() -> Path:
    """Return the session storage directory."""
    sessions_dir = get_data_dir() / "sessions"
    private_directory(sessions_dir)
    return sessions_dir


def get_project_config_dir(cwd: str | Path) -> Path:
    """Return the per-project .researchx directory."""
    project_dir = Path(cwd).resolve() / ".researchx"
    private_directory(project_dir)
    return project_dir


def get_data_path() -> Path:
    """Return ResearchX' data directory.

    This is a backwards-compatible alias used by older channel code.
    """

    return get_data_dir()
