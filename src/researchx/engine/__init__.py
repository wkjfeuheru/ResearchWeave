"""Core engine exports."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from researchx.engine.messages import (
        ConversationMessage,
        ImageBlock,
        TextBlock,
        ToolResultBlock,
        ToolUseBlock,
    )
    from researchx.engine.query_engine import QueryEngine
    from researchx.engine.stream_events import (
        AssistantTextDelta,
        AssistantTurnComplete,
        ToolExecutionCompleted,
        ToolExecutionStarted,
    )

__all__ = [
    "AssistantTextDelta",
    "AssistantTurnComplete",
    "ConversationMessage",
    "ImageBlock",
    "QueryEngine",
    "TextBlock",
    "ToolExecutionCompleted",
    "ToolExecutionStarted",
    "ToolResultBlock",
    "ToolUseBlock",
]


def __getattr__(name: str) -> object:
    if name in {
        "ConversationMessage",
        "ImageBlock",
        "TextBlock",
        "ToolResultBlock",
        "ToolUseBlock",
    }:
        from researchx.engine.messages import (
            ConversationMessage,
            ImageBlock,
            TextBlock,
            ToolResultBlock,
            ToolUseBlock,
        )

        return {
            "ConversationMessage": ConversationMessage,
            "ImageBlock": ImageBlock,
            "TextBlock": TextBlock,
            "ToolResultBlock": ToolResultBlock,
            "ToolUseBlock": ToolUseBlock,
        }[name]

    if name == "QueryEngine":
        from researchx.engine.query_engine import QueryEngine

        return QueryEngine

    if name in {
        "AssistantTextDelta",
        "AssistantTurnComplete",
        "ToolExecutionCompleted",
        "ToolExecutionStarted",
    }:
        from researchx.engine.stream_events import (
            AssistantTextDelta,
            AssistantTurnComplete,
            ToolExecutionCompleted,
            ToolExecutionStarted,
        )

        return {
            "AssistantTextDelta": AssistantTextDelta,
            "AssistantTurnComplete": AssistantTurnComplete,
            "ToolExecutionCompleted": ToolExecutionCompleted,
            "ToolExecutionStarted": ToolExecutionStarted,
        }[name]

    raise AttributeError(name)
