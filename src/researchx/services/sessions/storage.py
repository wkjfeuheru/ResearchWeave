"""Session persistence helpers."""

from __future__ import annotations
from researchx.engine.metadata import ExecutionMetadata

import json
import time
from hashlib import sha1
from pathlib import Path
from typing import Mapping, cast, Any
from uuid import uuid4

from researchx.api.usage import UsageSnapshot
from researchx.config.paths import get_sessions_dir
from researchx.engine.messages import (
    ToolResultBlock,
    ConversationMessage,
    sanitize_conversation_messages,
)
from researchx.storage.filesystem import atomic_write_text, private_directory


_PERSISTED_TOOL_METADATA_KEYS = (
    "permission_mode",
    "invoked_skills",
    "compact_checkpoints",
    "compact_last",
    "session_approvals",
    "context_budget",
)


def _sanitize_metadata(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _sanitize_metadata(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_sanitize_metadata(item) for item in value]
    return str(value)


def _persistable_tool_metadata(tool_metadata: Mapping[str, object] | None) -> ExecutionMetadata:
    if not isinstance(tool_metadata, dict):
        return {}
    payload: dict[str, Any] = {}
    for key in _PERSISTED_TOOL_METADATA_KEYS:
        if key in tool_metadata:
            if key == "session_approvals":
                approvals = tool_metadata[key]
                if isinstance(approvals, dict):
                    payload[key] = {
                        scope: (
                            list(
                                dict.fromkeys(
                                    item for item in items if isinstance(item, str) and item
                                )
                            )
                        )
                        for scope in ("tools", "edit_paths")
                        if isinstance(items := approvals.get(scope), list)
                    }
            else:
                payload[key] = _sanitize_metadata(tool_metadata[key])
    return cast(ExecutionMetadata, payload)


def get_project_session_dir(cwd: str | Path) -> Path:
    """Return the session directory for a project."""
    path = Path(cwd).resolve()
    digest = sha1(str(path).encode("utf-8")).hexdigest()[:12]
    session_dir = get_sessions_dir() / f"{path.name}-{digest}"
    private_directory(session_dir)
    return session_dir


async def save_session_snapshot(
    *,
    cwd: str | Path,
    model: str,
    system_prompt: str,
    messages: list[ConversationMessage],
    usage: UsageSnapshot,
    session_id: str | None = None,
    tool_metadata: Mapping[str, object] | None = None,
) -> Path:
    """Persist conversation rows; return a stable export locator, not a JSON snapshot."""
    session_dir = get_project_session_dir(cwd)
    sid = session_id or uuid4().hex[:12]
    now = time.time()
    messages = sanitize_conversation_messages(messages)
    # Extract a summary from the first user message
    summary = ""
    for msg in messages:
        if msg.role == "user" and msg.text.strip():
            summary = msg.text.strip()[:80]
            break

    payload = {
        "session_id": sid,
        "cwd": str(Path(cwd).resolve()),
        "model": model,
        "system_prompt": system_prompt,
        "messages": [message.model_dump(mode="json") for message in messages],
        "usage": usage.model_dump(),
        "tool_metadata": (_persistable_tool_metadata(tool_metadata)),
        "created_at": now,
        "summary": summary,
        "message_count": len(messages),
    }
    from researchx.storage.conversations import ConversationRecords
    from researchx.storage.database import current_database

    await ConversationRecords(current_database(), str(cwd)).write(payload, channel="cli")
    return session_dir / sid


def _sanitize_snapshot_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize persisted messages for forward compatibility."""
    payload = dict(payload)
    payload["tool_metadata"] = _persistable_tool_metadata(payload.get("tool_metadata"))
    raw_messages = payload.get("messages", [])
    if isinstance(raw_messages, list):
        messages = sanitize_conversation_messages(
            [ConversationMessage.model_validate(item) for item in raw_messages]
        )
        payload = dict(payload)
        payload["messages"] = [message.model_dump(mode="json") for message in messages]
        payload["message_count"] = len(messages)
    return payload


async def load_session_snapshot(cwd: str | Path) -> dict[str, Any] | None:
    from researchx.storage.conversations import ConversationRecords
    from researchx.storage.database import current_database

    records = await ConversationRecords(current_database(), str(cwd)).list(channel="cli", limit=1)
    return _sanitize_snapshot_payload(records[0]) if records else None


async def list_session_snapshots(cwd: str | Path, limit: int = 20) -> list[dict[str, Any]]:
    from researchx.storage.conversations import ConversationRecords
    from researchx.storage.database import current_database

    records = await ConversationRecords(current_database(), str(cwd)).list(
        channel="cli", limit=limit
    )
    return [
        {
            key: record.get(key)
            for key in ("session_id", "summary", "message_count", "model", "created_at")
        }
        for record in records
    ]


async def load_session_by_id(cwd: str | Path, session_id: str) -> dict[str, Any] | None:
    from researchx.storage.conversations import ConversationRecords
    from researchx.storage.database import current_database

    if session_id == "latest":
        return await load_session_snapshot(cwd)
    record = await ConversationRecords(current_database(), str(cwd)).load(session_id)
    return _sanitize_snapshot_payload(record) if record else None


def export_session_markdown(
    *,
    cwd: str | Path,
    messages: list[ConversationMessage],
) -> Path:
    """Export the session transcript as Markdown."""
    session_dir = get_project_session_dir(cwd)
    path = session_dir / "transcript.md"
    parts: list[str] = ["# ResearchX Session Transcript"]
    for message in messages:
        parts.append(f"\n## {message.role.capitalize()}\n")
        text = message.text.strip()
        if text:
            parts.append(text)
        for block in message.tool_uses:
            parts.append(
                f"\n```tool\n{block.name} {json.dumps(block.input, ensure_ascii=True)}\n```"
            )
        for result_block in message.content:
            if isinstance(result_block, ToolResultBlock):
                parts.append(f"\n```tool-result\n{result_block.content}\n```")
    atomic_write_text(path, "\n".join(parts).strip() + "\n")
    return path
