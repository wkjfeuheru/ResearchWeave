"""Session file metadata in PostgreSQL; immutable bodies in ContentStore."""

from __future__ import annotations

import asyncio
import builtins
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, cast
from typing_extensions import TypedDict
from uuid import uuid4
from sqlalchemy import select, delete
from sqlalchemy.dialects.postgresql import insert
from researchx.engine.metadata import ExecutionLease
from researchx.storage import schema as s
from researchx.storage.database import current_database
from researchx.storage.research_records import scope
from researchx.storage.filesystem import atomic_write_bytes, atomic_write_text, private_directory
from researchx.workspace.documents import MAX_DOCUMENT_BYTES, document_text, parse_document

if TYPE_CHECKING:
    from researchx.state.store import ResearchStore


class FileManifest(TypedDict, total=False):
    id: str
    name: str
    size: int
    status: str
    gaps: list[str]
    original: str
    document_hash: str
    filename: str
    type: str
    kind: str
    task_id: str | None
    created_at: str
    execution_id: str
    task_revision: int
    plan_revision: int
    objective_revision: int
    stale: bool


class SessionFiles:
    def __init__(self, store: ResearchStore) -> None:
        self.store = store
        self.root = store.directory.resolve() / "files"
        private_directory(self.root)

    def _id(self, value: str) -> str:
        if not re.fullmatch(r"[a-f0-9]{32}", value):
            raise ValueError("无效的文件ID")
        return value

    def _path(self, group: str, value: str) -> Path:
        return self.root / group / self._id(value)

    def _safe(self, directory: Path, filename: str) -> Path:
        path = directory / filename
        if not path.resolve().is_relative_to(
            directory.resolve()
        ) or not path.resolve().is_relative_to(self.root):
            raise ValueError("文件路径不属于当前会话")
        if any(
            part.is_symlink() for part in (path, *path.parents) if part.is_relative_to(self.root)
        ):
            raise ValueError("会话内容路径不允许符号链接")
        return path

    async def _write(self, group: str, meta: FileManifest, content: bytes) -> None:
        reference = await self.store.content.put(self.store.workspace_id, content)
        async with self.store.transaction() as db:
            await self.store.records.register_content(db, self.store.content, [reference])
            await db.execute(
                insert(s.file_records).values(
                    workspace_id=self.store.workspace_id,
                    session_id=self.store.session_id,
                    record_id=meta["id"],
                    kind=group,
                    content_hash=reference.content_hash,
                    payload=dict(meta),
                )
            )

    async def _record(self, group: str, identifier: str) -> tuple[FileManifest, str]:
        self._id(identifier)
        async with current_database().transaction() as db:
            row = (
                (
                    await db.execute(
                        select(s.file_records).where(
                            scope(s.file_records, self.store.workspace_id, self.store.session_id),
                            s.file_records.c.record_id == identifier,
                            s.file_records.c.kind == group,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise FileNotFoundError("文件不存在或不属于当前会话")
            return cast(FileManifest, row["payload"]), row["content_hash"]

    async def upload(self, name: str, content: bytes) -> FileManifest:
        if len(content) > MAX_DOCUMENT_BYTES:
            raise ValueError("文件超过30 MB限制")
        display = Path(name.replace("\\", "/")).name[:200]
        suffix = Path(display).suffix.lower()
        if suffix not in {".pdf", ".txt", ".md"}:
            raise ValueError("仅支持PDF、TXT、MD")
        identifier = uuid4().hex
        directory = self._path("attachments", identifier)
        private_directory(directory)
        path = self._safe(directory, "original" + suffix)
        await asyncio.to_thread(atomic_write_bytes, path, content, mode=0o600)
        try:
            parsed = await asyncio.to_thread(parse_document, path)
            # These are derived document bodies for the agent, never authority for metadata.
            atomic_write_text(
                directory / "parsed.json", json.dumps(parsed, ensure_ascii=False), mode=0o600
            )
            atomic_write_text(directory / "text.md", document_text(parsed), mode=0o600)
            meta: FileManifest = {
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
        await self._write("attachments", meta, content)
        return meta

    async def list(self, group: str) -> builtins.list[FileManifest]:
        if group not in {"attachments", "artifacts"}:
            raise ValueError("未知文件分组")
        async with current_database().transaction() as db:
            rows = list(
                (
                    await db.scalars(
                        select(s.file_records.c.payload).where(
                            scope(s.file_records, self.store.workspace_id, self.store.session_id),
                            s.file_records.c.kind == group,
                        )
                    )
                ).all()
            )
        items = [
            await self._execution_projection(cast(FileManifest, row))
            if group == "artifacts"
            else cast(FileManifest, row)
            for row in rows
        ]
        return sorted(items, key=lambda item: (item.get("created_at", ""), item["id"]))

    async def attachment(self, identifier: str) -> tuple[FileManifest, Path]:
        meta, digest = await self._record("attachments", identifier)
        reference = await self.store._content_reference(digest)
        body = await self.store.content.read(reference)
        directory = self._path("attachments", identifier)
        private_directory(directory)
        original = self._safe(directory, meta["original"])
        # Working files are disposable projections, including after a legacy import
        # or a move to another host sharing the ContentStore.
        await asyncio.to_thread(atomic_write_bytes, original, body, mode=0o600)
        if meta["status"] != "failed":
            parsed = await asyncio.to_thread(parse_document, original)
            await asyncio.to_thread(
                atomic_write_text,
                self._safe(directory, "parsed.json"),
                json.dumps(parsed, ensure_ascii=False),
                mode=0o600,
            )
            await asyncio.to_thread(
                atomic_write_text,
                self._safe(directory, "text.md"),
                document_text(parsed),
                mode=0o600,
            )
        return meta, directory

    async def describe(self, identifiers: builtins.list[str]) -> str:
        lines = []
        for identifier in identifiers:
            meta, directory = await self.attachment(identifier)
            original = self._safe(directory, meta["original"])
            lines.append(
                f"附件 {meta['name']} (ID {identifier}, 状态 {meta['status']}):\n"
                f"原文件: {original}\n按页/行定位的文本: {directory / 'text.md'}\n"
                f"解析索引: {directory / 'parsed.json'}\n缺口: {meta['gaps']}"
            )
        return "\n\n".join(lines)

    async def delete_attachment(self, identifier: str) -> None:
        await self._record("attachments", identifier)
        async with self.store.transaction() as db:
            await db.execute(
                delete(s.file_records).where(
                    scope(s.file_records, self.store.workspace_id, self.store.session_id),
                    s.file_records.c.record_id == identifier,
                    s.file_records.c.kind == "attachments",
                )
            )
        # Retain immutable objects until an explicit, reference-aware retention operation.

    async def artifact(self, identifier: str) -> tuple[FileManifest, Path]:
        meta, digest = await self._record("artifacts", identifier)
        reference = await self.store._content_reference(digest)
        data = await self.store.content.read(reference)
        directory = self._path("artifacts", identifier)
        private_directory(directory)
        path = self._safe(directory, meta["filename"])
        # A verified materialization supports FileResponse and existing download filenames.
        await asyncio.to_thread(atomic_write_bytes, path, data, mode=0o600)
        return await self._execution_projection(meta), path

    async def _execution_projection(self, data: FileManifest) -> FileManifest:
        if not data.get("execution_id"):
            return data
        memory = await self.store.load()
        execution = memory.executions.get(data["execution_id"])
        plan = memory.plans.get(memory.research_state.current_plan_id or "")
        task = (
            next((item for item in plan.tasks if item.id == data.get("task_id")), None)
            if plan
            else None
        )
        stale = (
            execution is None
            or execution.status not in {"running", "committed"}
            or task is None
            or task.status in {"cancelled", "failed"}
            or task.task_revision != data.get("task_revision")
            or any(item.file_id == data["id"] and item.stale for item in memory.artifacts.values())
        )
        pending = execution is not None and execution.status == "running"
        return {
            **data,
            "stale": stale,
            "status": "stale" if stale else "pending_execution" if pending else data["status"],
        }

    async def register(
        self,
        path: Path,
        *,
        task_id: str | None,
        status: str,
        kind: str,
        execution: ExecutionLease | None = None,
    ) -> FileManifest:
        content = await asyncio.to_thread(path.read_bytes)
        identifier = uuid4().hex
        data: FileManifest = {
            "id": identifier,
            "name": path.name,
            "filename": "report" + path.suffix,
            "type": path.suffix[1:],
            "kind": kind,
            "status": status,
            "task_id": task_id,
            "size": len(content),
            "created_at": datetime.now(timezone.utc).isoformat(),
            **(
                {
                    "execution_id": execution["id"],
                    "task_revision": execution["task_revision"],
                    "plan_revision": execution["plan_revision"],
                    "objective_revision": execution["objective_revision"],
                }
                if execution
                else {}
            ),
        }
        await self._write("artifacts", data, content)
        return data
