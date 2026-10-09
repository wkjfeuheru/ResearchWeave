"""Provider-neutral captured source contract used at the host commit boundary."""

from typing import TypedDict, Literal


class RequiredSourceSpec(TypedDict):
    content: str


class SourceSpec(RequiredSourceSpec, total=False):
    kind: Literal["user", "tool", "web", "file", "search", "mcp", "calculation"]
    title: str
    locator: str
    published_at: str | None
    fragment: bool
