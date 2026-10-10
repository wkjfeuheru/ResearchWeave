"""Service exports."""

from typing import TYPE_CHECKING
from importlib import import_module

if TYPE_CHECKING:
    from researchx.services.compact import (
        build_post_compact_messages,
        compact_conversation,
        compact_messages,
        estimate_conversation_tokens,
        summarize_messages,
    )
    from researchx.services.sessions.storage import (
        export_session_markdown,
        get_project_session_dir,
        load_session_snapshot,
        save_session_snapshot,
    )
    from researchx.services.context.token_estimation import estimate_message_tokens, estimate_tokens

__all__ = [
    "compact_messages",
    "compact_conversation",
    "build_post_compact_messages",
    "estimate_conversation_tokens",
    "estimate_message_tokens",
    "estimate_tokens",
    "export_session_markdown",
    "get_project_session_dir",
    "load_session_snapshot",
    "save_session_snapshot",
    "summarize_messages",
]


def __getattr__(name: str) -> object:
    if name not in __all__:
        raise AttributeError(name)
    if name in {"estimate_message_tokens", "estimate_tokens"}:
        module = "researchx.services.context.token_estimation"
    elif name in {
        "export_session_markdown",
        "get_project_session_dir",
        "load_session_snapshot",
        "save_session_snapshot",
    }:
        module = "researchx.services.sessions.storage"
    else:
        module = "researchx.services.compact"
    value: object = getattr(import_module(module), name)
    globals()[name] = value
    return value
