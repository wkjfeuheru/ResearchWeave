"""Project persisted conversation messages into the browser session view."""

from __future__ import annotations

from typing import cast
from researchx.engine.messages import ConversationMessage, TextBlock, ToolUseBlock, ToolResultBlock
from researchx.state.models import AnswerReceipt
from researchx.web.types import BrowserRow, WebSessionRecord, SessionView
from researchx.web.citations import render_web_answer


def session_view(record: WebSessionRecord) -> SessionView:
    """Render persisted engine messages without exposing internal runtime metadata."""
    rows: list[BrowserRow] = []
    names: dict[str, str] = {}
    for raw_message in record["messages"]:
        message = ConversationMessage.model_validate(raw_message)
        for index, block in enumerate(message.content):
            row_id = f"{len(rows)}-{index}"
            if isinstance(block, TextBlock) and block.text:
                frozen = message.research_citations if message.role == "assistant" else None
                rows.append(
                    {
                        "id": row_id,
                        "role": message.role,
                        "text": render_web_answer(cast(AnswerReceipt, frozen))
                        if frozen
                        else block.text,
                    }
                )
            elif isinstance(block, ToolUseBlock):
                names[block.id] = block.name
                rows.append(
                    {
                        "id": row_id,
                        "role": "tool",
                        "text": "",
                        "tool_name": block.name,
                        "tool_input": block.input,
                    }
                )
            elif isinstance(block, ToolResultBlock):
                rows.append(
                    {
                        "id": row_id,
                        "role": "tool_result",
                        "text": block.content,
                        "tool_name": names.get(block.tool_use_id, "工具"),
                        "is_error": block.is_error,
                    }
                )
    rows = [
        row
        for row in record.get("display_messages", rows)
        if row["role"] not in {"tool", "tool_result"}
    ]
    return cast(
        SessionView,
        {
            k: record[k]
            for k in ("session_id", "profile_id", "model", "summary", "created_at", "updated_at")
        }
        | {
            "messages": rows,
            "usage": record.get("usage", {}),
            "research_progress": record.get("research_progress"),
        },
    )
