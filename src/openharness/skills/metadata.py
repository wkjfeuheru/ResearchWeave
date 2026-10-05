"""Stable skill fingerprints and backwards-compatible metadata parsing."""

from hashlib import sha256
from pathlib import Path
import json

from openharness.skills._frontmatter import parse_skill_metadata
from openharness.skills.types import SkillMetadata


def content_hash(entrypoint: Path) -> str:
    entrypoint = entrypoint.resolve()
    base = entrypoint.parent.resolve()
    paths = [entrypoint]
    if entrypoint.name == "SKILL.md":
        for name in ("references", "scripts", "templates", "assets"):
            root = base / name
            if root.is_dir() and root.resolve().is_relative_to(base):
                paths.extend(
                    path
                    for path in root.rglob("*")
                    if path.is_file()
                    and path.resolve().is_relative_to(base)
                    and not any(
                        part in {"__pycache__", "tests", ".pytest_cache", ".cache"}
                        for part in path.relative_to(base).parts
                    )
                    and path.suffix not in {".pyc", ".pyo"}
                )
    digest = sha256()
    for path in sorted(paths, key=lambda item: item.relative_to(base).as_posix()):
        data = path.read_bytes()
        if path.suffix == ".md":
            text = data.decode("utf-8")
            if text.startswith("---\n") and (end := text.find("\n---\n", 4)) != -1:
                frontmatter = parse_skill_metadata(path.stem, text)["frontmatter"]
                frontmatter.pop("content_hash", None)
                if isinstance(frontmatter.get("metadata"), dict):
                    frontmatter["metadata"].pop("content_hash", None)
                data = (
                    json.dumps(frontmatter, sort_keys=True, ensure_ascii=False, default=str)
                    + text[end + 5 :]
                ).encode()
        for value in (path.relative_to(base).as_posix().encode(), data):
            digest.update(len(value).to_bytes(8, "big"))
            digest.update(value)
    return digest.hexdigest()


def read_metadata(name: str, frontmatter: dict, entrypoint: Path) -> SkillMetadata:
    nested = frontmatter.get("metadata")
    values = dict(nested) if isinstance(nested, dict) else {}
    values.update(
        {key: frontmatter[key] for key in SkillMetadata.model_fields if key in frontmatter}
    )
    values.setdefault("skill_id", name)
    if hasattr(values.get("published_at"), "isoformat"):
        values["published_at"] = values["published_at"].isoformat()
    values["content_hash"] = content_hash(entrypoint)
    return SkillMetadata.model_validate(values)
