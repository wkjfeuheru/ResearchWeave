"""Simple usage aggregation."""

from __future__ import annotations

from openharness.api.usage import UsageSnapshot


class CostTracker:
    """Accumulate usage over the lifetime of a session."""

    def __init__(self, usage: UsageSnapshot | None = None) -> None:
        self._usage = usage or UsageSnapshot()

    def add(self, usage: UsageSnapshot) -> None:
        """Add a usage snapshot to the running total."""
        self._usage = UsageSnapshot(
            input_tokens=self._usage.input_tokens + usage.input_tokens,
            output_tokens=self._usage.output_tokens + usage.output_tokens,
            cache_observed_input_tokens=self._usage.cache_observed_input_tokens
            + usage.cache_observed_input_tokens,
            cache_read_input_tokens=_add_optional(
                self._usage.cache_read_input_tokens, usage.cache_read_input_tokens
            ),
            cache_creation_input_tokens=_add_optional(
                self._usage.cache_creation_input_tokens, usage.cache_creation_input_tokens
            ),
        )

    @property
    def total(self) -> UsageSnapshot:
        """Return the aggregated usage."""
        return self._usage


def _add_optional(left: int | None, right: int | None) -> int | None:
    return None if left is None and right is None else (left or 0) + (right or 0)
