"""PostgreSQL context archives and verified ContentStore recovery projections."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from sqlalchemy import insert

from researchx.engine.metadata import ExecutionMetadata
from researchx.engine.messages import ConversationMessage
from researchx.config.paths import get_data_dir
from researchx.storage import schema as s
from researchx.storage.content import persist_object, materialize_object
from researchx.storage.conversations import ConversationRecords
from researchx.storage.database import current_database, workspace_id
from researchx.storage.research_records import ensure_session


def _scope(metadata: ExecutionMetadata | None = None) -> tuple[str, str]:
    from researchx.api.retry import _api_scope

    store = (metadata or {}).get("research_store")
    if store is not None:
        return store.cwd, store.session_id
    active = _api_scope.get()
    return active or (str(Path.cwd()), (metadata or {}).get("session_id") or "context-service")


async def save_context_snapshot(
    messages: list[ConversationMessage],
    *,
    model: str = "",
    metadata: ExecutionMetadata | None = None,
) -> Path:
    from researchx.services.sessions.storage import _persistable_tool_metadata

    cwd, parent = _scope(metadata)
    database, workspace = current_database(), workspace_id(cwd)
    identity = uuid4().hex
    record = {
        "session_id": "archive-" + identity,
        "parent_session_id": parent,
        "cwd": cwd,
        "model": model,
        "messages": [message.model_dump(mode="json") for message in messages],
        "tool_metadata": _persistable_tool_metadata(metadata),
    }
    payload = {
        "version": 1,
        "model": model,
        "messages": record["messages"],
        "metadata": record["tool_metadata"],
        "session_id": parent,
    }
    async with database.transaction() as db:
        await ensure_session(db, workspace, parent, cwd)
        key = await persist_object(
            database, workspace, json.dumps(payload, ensure_ascii=False).encode()
        )
        await ConversationRecords(database, cwd).write(record, channel="context_archive")
        await db.execute(
            insert(s.file_records).values(
                workspace_id=workspace,
                session_id=record["session_id"],
                record_id=identity,
                kind="context_archive",
                content_hash=key.removeprefix("sha256:"),
                payload={"parent_session_id": parent, "model": model, "format": "json"},
            )
        )
    # This is an explicit backup projection for existing file-based inspection, never a backend.
    return await materialize_object(
        database, workspace, key, get_data_dir() / "context_snapshots", identity + ".json"
    )


async def save_tool_content(content: str) -> Path:
    cwd, session = _scope()
    database, workspace = current_database(), workspace_id(cwd)
    async with database.transaction() as db:
        await ensure_session(db, workspace, session, cwd)
        key = await persist_object(database, workspace, content.encode())
    return await materialize_object(
        database,
        workspace,
        key,
        get_data_dir() / "tool_artifacts",
        "context-" + key.removeprefix("sha256:") + ".txt",
    )
