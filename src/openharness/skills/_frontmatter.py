"""Shared YAML frontmatter parsing for SKILL.md files."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


def read_discovery_header(path: Path) -> str:
    """Read frontmatter only; legacy Markdown gets a bounded heading/paragraph probe.

    Binary readline avoids text buffering decoding a potentially huge body. Invalid
    or unterminated headers are bounded too; no discovery read exceeds 64 KiB.
    """
    with path.open("rb", buffering=0) as stream:
        first = stream.readline(4096)
        lines = [first]
        total = len(first)
        if first.rstrip(b"\r\n") == b"---":
            while total < 65536:
                line = stream.readline(min(4096, 65536 - total))
                if not line:
                    break
                lines.append(line)
                total += len(line)
                if line.rstrip(b"\r\n") == b"---":
                    break
        else:
            # Preserve heading-derived legacy names and the first short paragraph.
            for _ in range(31):
                if total >= 65536:
                    break
                line = stream.readline(min(4096, 65536 - total))
                if not line:
                    break
                lines.append(line)
                total += len(line)
                if line.strip() and not line.lstrip().startswith(b"#"):
                    break
    return b"".join(lines).decode("utf-8", errors="replace").replace("\r\n", "\n")


def parse_bool_frontmatter(value: Any, *, default: bool) -> bool:
    """Parse permissive YAML frontmatter booleans."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def optional_frontmatter_str(value: Any) -> str | None:
    """Return a stripped string value, or ``None`` when absent/blank."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def parse_skill_metadata(
    default_name: str,
    content: str,
    *,
    fallback_template: str = "Skill: {name}",
) -> dict[str, Any]:
    """Extract metadata from a SKILL.md file.

    Parses YAML frontmatter (``---`` delimited) via ``yaml.safe_load`` so that
    folded block scalars (``>``), literal block scalars (``|``), quoted values,
    and other standard YAML constructs are handled correctly. Falls back to
    ``# heading`` + first body paragraph when no usable frontmatter is present,
    and finally to ``fallback_template`` when no description can be derived.
    """
    name = default_name
    description = ""
    frontmatter: dict[str, Any] = {}
    lines = content.splitlines()

    if content.startswith("---\n"):
        end_index = content.find("\n---\n", 4)
        if end_index != -1:
            try:
                metadata = yaml.safe_load(content[4:end_index])
                if isinstance(metadata, dict):
                    frontmatter = metadata
                    val = metadata.get("name")
                    if isinstance(val, str) and val.strip():
                        name = val.strip()
                    val = metadata.get("description")
                    if isinstance(val, str) and val.strip():
                        description = val.strip()
            except yaml.YAMLError:
                logger.debug("Failed to parse YAML frontmatter for skill %s", default_name)

    if not description:
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("# "):
                if not name or name == default_name:
                    name = stripped[2:].strip() or default_name
                continue
            if stripped and not stripped.startswith("---") and not stripped.startswith("#"):
                description = stripped[:200]
                break

    if not description:
        description = fallback_template.format(name=name)
    return {"name": name, "description": description, "frontmatter": frontmatter}


def parse_skill_frontmatter(
    default_name: str,
    content: str,
    *,
    fallback_template: str = "Skill: {name}",
) -> tuple[str, str]:
    """Extract ``name`` and ``description`` from a SKILL.md file."""
    metadata = parse_skill_metadata(
        default_name,
        content,
        fallback_template=fallback_template,
    )
    return str(metadata["name"]), str(metadata["description"])
