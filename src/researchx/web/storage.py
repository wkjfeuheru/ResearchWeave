"""Persistent browser conversations and research progress."""

from __future__ import annotations
from typing import cast
from researchx.api.usage import UsageSnapshot
from researchx.engine.metadata import ExecutionMetadata
from researchx.web.types import WebSessionRecord

import re
import time
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from researchx.config.paths import get_data_dir
from researchx.engine.messages import ConversationMessage, sanitize_conversation_messages
from researchx.state.store import ResearchStore
from researchx.services.sessions.storage import _persistable_tool_metadata
from researchx.storage.filesystem import atomic_write_text, private_directory
from researchx.web.citations import project_answer_rows
from researchx.storage.conversations import ConversationRecords
from researchx.storage.database import current_database


class WebSessionBackend:
    def __init__(self, cwd: str) -> None:
        self.cwd = str(Path(cwd).resolve())
        digest = sha256(self.cwd.encode()).hexdigest()[:16]
        self.directory = get_data_dir() / "web" / "sessions" / digest
        private_directory(self.directory)

    def get_session_dir(self, cwd: str | Path) -> Path:
        return self.directory

    def _path(self, session_id: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{12}", session_id):
            raise ValueError("无效的会话 ID")
        return self.directory / session_id

    async def create(self, profile_id: str) -> WebSessionRecord:
        now = time.time()
        record: WebSessionRecord = {
            "session_id": uuid4().hex[:12],
            "source": "web",
            "cwd": self.cwd,
            "profile_id": profile_id,
            "model": "",
            "summary": "新对话",
            "created_at": now,
            "updated_at": now,
            "messages": [],
            "tool_metadata": {},
            "usage": {},
            "message_count": 0,
        }
        (await self.write(record))
        return record

    @property
    def records(self) -> ConversationRecords:
        return ConversationRecords(current_database(), self.cwd)

    async def write(self, record: WebSessionRecord) -> None:
        self._path(record["session_id"])
        await self.records.write(dict(record), channel="web")

    async def delete(self, session_id: str) -> None:
        self._path(session_id)
        await self.records.delete(session_id)

    async def save_snapshot(
        self,
        *,
        cwd: str | Path,
        model: str,
        system_prompt: str,
        messages: list[ConversationMessage],
        usage: UsageSnapshot,
        session_id: str | None = None,
        tool_metadata: ExecutionMetadata | None = None,
    ) -> Path:
        record = (await self.load_by_id(cwd, session_id)) if session_id else None
        if record is None:
            raise ValueError("会话不存在")
        messages = sanitize_conversation_messages(messages)
        first_user = next((m.text for m in messages if m.role == "user" and m.text), "")
        record.update(
            {
                "model": model,
                "system_prompt": system_prompt,
                "messages": [m.model_dump(mode="json") for m in messages],
                "usage": usage.model_dump(mode="json"),
                "tool_metadata": (_persistable_tool_metadata(tool_metadata)),
                "summary": first_user[:60] or record["summary"],
                "message_count": len(messages),
                "updated_at": time.time(),
            }
        )
        (await self.write(record))
        return self._path(record["session_id"])

    async def load_by_id(
        self, cwd: str | Path, session_id: str, *, include_research_progress: bool = True
    ) -> WebSessionRecord | None:
        self._path(session_id)
        record = await self.records.load(session_id)
        if record is None:
            return None
        messages = sanitize_conversation_messages(
            [ConversationMessage.model_validate(m) for m in record["messages"]]
        )
        record["messages"] = [m.model_dump(mode="json") for m in messages]
        record["tool_metadata"] = _persistable_tool_metadata(record.get("tool_metadata"))
        if include_research_progress or record.get("display_messages"):
            research = ResearchStore(self.cwd, session_id)
            memory = await research.load()
            if record.get("display_messages"):
                record["display_messages"] = project_answer_rows(
                    record["display_messages"], memory.answers
                )
            if include_research_progress:
                record["research_progress"] = await research.progress(memory)
        return cast(WebSessionRecord, record)

    async def list_snapshots(self, cwd: str | Path, limit: int = 100) -> list[WebSessionRecord]:
        records = await self.records.list(channel="web", limit=limit)
        return [cast(WebSessionRecord, record) for record in records]

    async def load_latest(self, cwd: str | Path) -> WebSessionRecord | None:
        return next(iter((await self.list_snapshots(cwd, limit=1))), None)

    def export_markdown(self, *, cwd: str | Path, messages: list[ConversationMessage]) -> Path:
        path = self.directory / "conversation.md"
        atomic_write_text(path, "\n\n".join(f"## {m.role}\n\n{m.text}" for m in messages))
        return path
