"""Web session coordination: connection ownership, request locks and recovery projections."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING
from fastapi import HTTPException
from researchx.state.errors import ResearchError
from researchx.state.store import ResearchStore
from researchx.web.storage import WebSessionBackend
from researchx.web.types import WebSessionRecord, BrowserRow
from researchx.web.session_view import session_view

if TYPE_CHECKING:
    from researchx.web.runtime import SessionController


class WebWorkspace:
    def __init__(self, cwd: str) -> None:
        self.cwd = str(Path(cwd).resolve())
        self.store = WebSessionBackend(self.cwd)
        self.lock = asyncio.Lock()
        self.connections: dict[str, SessionController] = {}
        self.execution_owner: str | None = None
        self.command_ids: dict[str, dict[str, tuple[str, dict[str, object]]]] = {}
        self.deleting: set[str] = set()
        self.file_operations: set[str] = set()

    def record(self, session_id: str) -> WebSessionRecord:
        try:
            if session_id in self.deleting or not self.store._path(session_id).is_file():
                raise HTTPException(404, "会话不存在")
            connection = self.connections.get(session_id)
            idle = connection is None or not connection.busy
            if idle:
                ResearchStore(self.cwd, session_id).recover_pending_steers()
                ResearchStore(self.cwd, session_id).recover_investigations()
                from researchx.state.repository import ResearchRepository

                ResearchRepository(ResearchStore(self.cwd, session_id)).recover()
            record = self.store.load_by_id(self.cwd, session_id)
            if record is not None:
                # Acceptance is authoritative even if the process stopped
                # before the connection could save its display projection.
                pending = ResearchStore(self.cwd, session_id).load().pending_steers
                rows = list(session_view(record)["messages"])
                recovered = False
                if idle:
                    # A persisted running projection can survive a process crash.
                    # Opening history must not claim it is still executing.
                    for row in rows:
                        if row.get("turn_status") == "running":
                            row["turn_status"] = "stopped"
                            if row.get("phase") == "pending":
                                row["phase"] = "progress"
                            if row.get("status") == "running":
                                row["status"] = "interrupted"
                            recovered = True
                known = {row["id"] for row in rows}
                missing: list[BrowserRow] = [
                    {"id": key, "role": "user", "text": item["text"]}
                    for key, item in pending.items()
                    if key not in known
                ]
                if missing or recovered:
                    record["display_messages"] = rows + missing
                    self.store.write(record)
        except ResearchError as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError:
            record = None
        if record is None:
            raise HTTPException(404, "会话不存在")
        return record
