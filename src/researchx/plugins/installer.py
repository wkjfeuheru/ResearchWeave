"""Plugin installation helpers."""

from __future__ import annotations

import shutil
from pathlib import Path

from researchx.plugins.loader import get_user_plugins_dir


def _resolve_user_plugin_dir(name: str) -> Path:
    """Resolve a user plugin name to a direct child of the plugin directory."""
    if not name or name != Path(name).name or "\\" in name:
        raise ValueError("invalid plugin name")

    plugins_dir = get_user_plugins_dir().resolve()
    path = (plugins_dir / name).resolve()
    if path.parent != plugins_dir:
        raise ValueError("invalid plugin name")
    return path


def install_plugin_from_path(source: str | Path) -> Path:
    """Install a plugin directory into the user plugin directory."""
    src = Path(source).resolve()
    dest = get_user_plugins_dir() / src.name
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest)
    # Installation is an explicit resource operation, separate from L0 discovery.
    from researchx.skills.loader import load_skills_from_dirs
    from researchx.skills.metadata import content_hash

    for skill in load_skills_from_dirs([dest / "skills"], source="plugin", create_missing=False):
        try:
            content_hash(Path(skill.path or "."), verify=True)
        except (OSError, ValueError):
            pass  # A resource/hash issue must not break otherwise legitimate installation.
    return dest


def uninstall_plugin(name: str) -> bool:
    """Remove a user plugin by directory name."""
    path = _resolve_user_plugin_dir(name)
    if not path.exists():
        return False
    shutil.rmtree(path)
    return True
