"""Durable, private recovery artifacts written before context is reduced."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import uuid4

from openharness.config.paths import get_data_dir
from openharness.utils.fs import atomic_write_text


def save_context_snapshot(messages, *, model: str = "", metadata=None) -> Path:
    from openharness.services.session_storage import _persistable_tool_metadata

    payload = {"version": 1, "model": model,
               "messages": [message.model_dump(mode="json") for message in messages],
               "metadata": _persistable_tool_metadata(metadata)}
    # Session identity is safe to persist; live store/client objects are not.
    if metadata and isinstance(metadata.get("session_id"), str):
        payload["session_id"] = metadata["session_id"]
    path = get_data_dir() / "context_snapshots" / f"{uuid4().hex}.json"
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False), mode=0o600)
    return path


def save_tool_content(content: str) -> Path:
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    path = get_data_dir() / "tool_artifacts" / f"context-{digest}.txt"
    atomic_write_text(path, content, mode=0o600)
    return path
