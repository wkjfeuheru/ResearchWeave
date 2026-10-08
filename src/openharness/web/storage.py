"""Persistent browser conversations and research progress."""

from __future__ import annotations

import json
import re
import shutil
import time
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from openharness.config.paths import get_data_dir
from openharness.engine.messages import ConversationMessage, sanitize_conversation_messages
from openharness.research.store import ResearchStore
from openharness.services.session_storage import _persistable_tool_metadata
from openharness.utils.fs import atomic_write_text
from openharness.web.citations import project_answer_rows


class WebSessionBackend:
    def __init__(self, cwd: str) -> None:
        self.cwd = str(Path(cwd).resolve())
        digest = sha256(self.cwd.encode()).hexdigest()[:16]
        self.directory = get_data_dir() / "web" / "sessions" / digest
        self.directory.mkdir(parents=True, exist_ok=True)

    def get_session_dir(self, cwd: str | Path) -> Path:
        return self.directory

    def _path(self, session_id: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{12}", session_id):
            raise ValueError("无效的会话 ID")
        return self.directory / f"{session_id}.json"

    def create(self, profile_id: str) -> dict:
        now = time.time()
        record = {
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
        self.write(record)
        return record

    def write(self, record: dict) -> None:
        atomic_write_text(
            self._path(record["session_id"]),
            json.dumps(record, ensure_ascii=False, indent=2) + "\n",
            mode=0o600,
        )

    def delete(self, session_id: str) -> None:
        path = self._path(session_id)
        if not path.is_file():
            raise FileNotFoundError(session_id)
        research = ResearchStore(self.cwd, session_id).directory
        if research.exists():
            shutil.rmtree(research)
        path.unlink()

    def save_snapshot(
        self,
        *,
        cwd,
        model,
        system_prompt,
        messages,
        usage,
        session_id=None,
        tool_metadata=None,
    ) -> Path:
        record = self.load_by_id(cwd, session_id) if session_id else None
        if record is None:
            raise ValueError("会话不存在")
        messages = sanitize_conversation_messages(messages)
        first_user = next((m.text for m in messages if m.role == "user" and m.text), "")
        record.update(
            model=model,
            system_prompt=system_prompt,
            messages=[m.model_dump(mode="json") for m in messages],
            usage=usage.model_dump(mode="json"),
            tool_metadata=_persistable_tool_metadata(tool_metadata),
            summary=first_user[:60] or record["summary"],
            message_count=len(messages),
            updated_at=time.time(),
        )
        self.write(record)
        return self._path(record["session_id"])

    def load_by_id(self, cwd, session_id, *, include_research_progress: bool = True) -> dict | None:
        path = self._path(session_id)
        if not path.exists():
            return None
        record = json.loads(path.read_text(encoding="utf-8"))
        messages = sanitize_conversation_messages(
            [ConversationMessage.model_validate(m) for m in record["messages"]]
        )
        record["messages"] = [m.model_dump(mode="json") for m in messages]
        record["tool_metadata"] = _persistable_tool_metadata(record.get("tool_metadata"))
        if include_research_progress or record.get("display_messages"):
            research = ResearchStore(self.cwd, session_id)
            memory = research.load()
            if record.get("display_messages"):
                record["display_messages"] = project_answer_rows(record["display_messages"], memory.answers)
            if include_research_progress:
                record["research_progress"] = research.progress(memory)
        return record

    def list_snapshots(self, cwd, limit=100) -> list[dict]:
        records = []
        for path in self.directory.glob("*.json"):
            try:
                record = self.load_by_id(cwd, path.stem, include_research_progress=False)
                if record is not None:
                    records.append(record)
            except (ValueError, OSError):
                continue
        return sorted(records, key=lambda r: r["updated_at"], reverse=True)[:limit]

    def load_latest(self, cwd) -> dict | None:
        return next(iter(self.list_snapshots(cwd, limit=1)), None)

    def export_markdown(self, *, cwd, messages) -> Path:
        path = self.directory / "conversation.md"
        atomic_write_text(path, "\n\n".join(f"## {m.role}\n\n{m.text}" for m in messages))
        return path
