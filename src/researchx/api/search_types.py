"""Provider-neutral bounded search response; no transport or credential imports."""

from dataclasses import dataclass, field


@dataclass
class SearchBatch:
    candidates: list[dict[str, str]] = field(default_factory=list)
    error_code: str | None = None
    error: str | None = None
    request_id: str | None = None
