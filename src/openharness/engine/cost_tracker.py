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
            cache_observed_input_tokens=self._usage.cache_observed_input_tokens + usage.cache_observed_input_tokens,
            **{
                key: (None if getattr(self._usage, key) is None and getattr(usage, key) is None
                      else (getattr(self._usage, key) or 0) + (getattr(usage, key) or 0))
                for key in ("cache_read_input_tokens", "cache_creation_input_tokens")
            },
        )

    @property
    def total(self) -> UsageSnapshot:
        """Return the aggregated usage."""
        return self._usage
