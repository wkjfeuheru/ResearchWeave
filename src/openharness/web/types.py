"""Browser wire records, separate from authoritative research models."""

from typing_extensions import TypedDict
from openharness.engine.metadata import ExecutionMetadata


class BrowserRow(TypedDict, total=False):
    id: str
    role: str
    text: str
    turn_id: str
    turn_status: str
    phase: str
    status: str
    tool_name: str
    tool_input: dict[str, object]
    is_error: bool
    category: str
    label: str
    target: str
    outcome: str
    detail: str
    answer_id: str


class WebSessionRecord(TypedDict, total=False):
    session_id: str
    source: str
    cwd: str
    profile_id: str
    model: str
    summary: str
    created_at: float
    updated_at: float
    messages: list[dict[str, object]]
    display_messages: list[BrowserRow]
    tool_metadata: ExecutionMetadata
    usage: dict[str, object]
    message_count: int
    system_prompt: str
    research_progress: dict[str, object] | None


class SessionView(TypedDict, total=False):
    usage: dict[str, object]
    session_id: str
    profile_id: str
    model: str
    summary: str
    created_at: float
    updated_at: float
    messages: list[BrowserRow]
    research_progress: dict[str, object] | None
