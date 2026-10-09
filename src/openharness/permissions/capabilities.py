"""Host-owned capability bounds; never populated from tool metadata."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CapabilityContext:
    allowed: frozenset[str] = frozenset({"*"})

    def permits(self, required: frozenset[str]) -> bool:
        return "*" in self.allowed or required <= self.allowed

    def restrict(self, requested: frozenset[str]) -> CapabilityContext:
        if not self.permits(requested):
            raise ValueError("Child capabilities must be a subset of the parent")
        return CapabilityContext(requested)
