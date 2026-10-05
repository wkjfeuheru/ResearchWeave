"""Session-owned attachments and artifact manifests with opaque download IDs."""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from openharness.utils.file_lock import exclusive_file_lock
from openharness.utils.fs import atomic_write_text
from openharness.utils.research_documents import MAX_DOCUMENT_BYTES, document_text, parse_document


class SessionFiles:
    def __init__(self, session_directory: Path):
        self.root = session_directory.resolve() / "files"
        self.lock = self.root / ".files.lock"

    def _id(self, value: str) -> str:
        if not re.fullmatch(r"[a-f0-9]{32}", value):
            raise ValueError("无效的文件ID")
        return value

    def _path(self, group: str, value: str) -> Path:
        return self.root / group / self._id(value)

    def _safe(self, directory: Path, filename: str) -> Path:
        path = (directory / filename).resolve()
        if not path.is_relative_to(directory.resolve()) or not path.is_relative_to(self.root):
            raise ValueError("文件路径不属于当前会话")
        return path

    def upload(self, name: str, content: bytes) -> dict:
        if len(content) > MAX_DOCUMENT_BYTES:
            raise ValueError("文件超过30 MB限制")
        # Original filename is display-only; never used as a storage path.
        display = Path(name.replace("\\", "/")).name[:200]
        suffix = Path(display).suffix.lower()
        if suffix not in {".pdf", ".txt", ".md"}:
            raise ValueError("仅支持PDF、TXT、MD")
        identifier = uuid4().hex
        directory = self._path("attachments", identifier)
        directory.mkdir(parents=True, exist_ok=False)
        path = directory / ("original" + suffix)
        path.write_bytes(content)
        try:
            parsed = parse_document(path)
            atomic_write_text(directory / "parsed.json", json.dumps(parsed, ensure_ascii=False))
            atomic_write_text(directory / "text.md", document_text(parsed))
            meta = {
                "id": identifier,
                "name": display,
                "size": len(content),
                "status": parsed["status"],
                "document_hash": parsed["document_hash"],
                "gaps": parsed["gaps"],
                "original": path.name,
            }
        except ValueError as exc:
            meta = {
                "id": identifier,
                "name": display,
                "size": len(content),
                "status": "failed",
                "gaps": [str(exc)],
                "original": path.name,
            }
        meta["created_at"] = datetime.now(timezone.utc).isoformat()
        atomic_write_text(directory / "manifest.json", json.dumps(meta, ensure_ascii=False))
        return meta

    def list(self, group: str) -> list[dict]:
        if group not in {"attachments", "artifacts"}:
            raise ValueError("未知文件分组")
        items = []
        for path in sorted((self.root / group).glob("*/manifest.json")):
            if not path.resolve().is_relative_to(self.root):
                continue
            try:
                data = json.loads(path.read_text())
                if data["id"] == path.parent.name:
                    items.append(data)
            except (ValueError, KeyError, OSError):
                continue
        return sorted(items, key=lambda item: (item.get("created_at", ""), item["id"]))

    def attachment(self, identifier: str) -> tuple[dict, Path]:
        directory = self._path("attachments", identifier)
        path = self._safe(directory, "manifest.json")
        if not path.is_file():
            raise FileNotFoundError("附件不存在")
        return json.loads(path.read_text()), directory

    def describe(self, identifiers: list[str]) -> str:
        lines = []
        for identifier in identifiers:
            meta, directory = self.attachment(identifier)
            original = self._safe(directory, meta["original"])
            lines.append(
                f"附件 {meta['name']} (ID {identifier}, 状态 {meta['status']}):\n"
                f"原文件: {original}\n按页/行定位的文本: {directory / 'text.md'}\n"
                f"解析索引: {directory / 'parsed.json'}\n缺口: {meta['gaps']}"
            )
        return "\n\n".join(lines)

    def delete_attachment(self, identifier: str):
        _, directory = self.attachment(identifier)
        shutil.rmtree(directory)

    def artifact(self, identifier: str) -> tuple[dict, Path]:
        directory = self._path("artifacts", identifier)
        manifest = self._safe(directory, "manifest.json")
        if not manifest.is_file():
            raise FileNotFoundError("产物不存在")
        data = json.loads(manifest.read_text())
        path = self._safe(directory, data["filename"])
        if not path.is_file():
            raise FileNotFoundError("产物文件不存在")
        return data, path

    def register(self, path: Path, *, task_id: str | None, status: str, kind: str) -> dict:
        with exclusive_file_lock(self.lock):
            identifier = uuid4().hex
            directory = self._path("artifacts", identifier)
            directory.mkdir(parents=True)
            target = directory / ("report" + path.suffix)
            shutil.copyfile(path, target)
            data = {
                "id": identifier,
                "name": path.name,
                "filename": target.name,
                "type": path.suffix[1:],
                "kind": kind,
                "status": status,
                "task_id": task_id,
                "size": target.stat().st_size,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            atomic_write_text(directory / "manifest.json", json.dumps(data, ensure_ascii=False))
            return data
