"""Provider-neutral token accounting, including optional cache telemetry."""
from __future__ import annotations

from typing import Any
from pydantic import BaseModel


class UsageSnapshot(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    usage_reported: bool | None = None
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    # Only requests reporting cache reads contribute to the hit-rate denominator.
    cache_observed_input_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def cache_hit_rate(self) -> float | None:
        if self.cache_read_input_tokens is None or not self.cache_observed_input_tokens:
            return None
        return self.cache_read_input_tokens / self.cache_observed_input_tokens

    def cache_summary(self) -> str:
        read = self.cache_read_input_tokens
        write = self.cache_creation_input_tokens
        rate = self.cache_hit_rate
        partial = self.cache_observed_input_tokens < self.input_tokens
        return (
            f"Cache read: {read if read is not None else 'not provided'}; "
            f"write: {write if write is not None else 'not provided'}; "
            f"hit rate: {f'{rate:.1%}' if rate is not None else 'unknown'}"
            + (" (partial statistics)" if partial and read is not None else "")
        )


def _field(value: Any, name: str, default: Any = None) -> Any:
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def usage_from_provider(value: Any, provider: str) -> UsageSnapshot:
    """Normalize a final/cumulative provider snapshot; absent is distinct from zero."""
    output = int(_field(value, 'output_tokens' if provider != 'openai' else 'completion_tokens', 0) or 0)
    total = int(_field(value, 'input_tokens' if provider != 'openai' else 'prompt_tokens', 0) or 0)
    if provider == 'anthropic':
        read = _field(value, 'cache_read_input_tokens')
        write = _field(value, 'cache_creation_input_tokens')
        total += int(read or 0) + int(write or 0)
    else:
        details = _field(value, 'input_tokens_details' if provider == 'responses' else 'prompt_tokens_details')
        read = _field(details, 'cached_tokens')
        if read is None and provider == 'openai':
            read = _field(value, 'prompt_cache_hit_tokens')
        write = _field(details, 'cache_write_tokens')
    return UsageSnapshot(
        input_tokens=total, output_tokens=output,
        usage_reported=value is not None and (
            _field(value, 'prompt_tokens' if provider == 'openai' else 'input_tokens') is not None
            or _field(value, 'completion_tokens' if provider == 'openai' else 'output_tokens') is not None
        ),
        cache_read_input_tokens=int(read) if read is not None else None,
        cache_creation_input_tokens=int(write) if write is not None else None,
        cache_observed_input_tokens=total if read is not None else 0,
    )
