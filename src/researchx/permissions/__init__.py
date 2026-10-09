"""Permission helpers for ResearchX."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from researchx.permissions.checker import PermissionChecker, PermissionDecision
    from researchx.permissions.modes import PermissionMode

__all__ = ["PermissionChecker", "PermissionDecision", "PermissionMode"]


def __getattr__(name: str) -> object:
    if name in {"PermissionChecker", "PermissionDecision"}:
        from researchx.permissions.checker import PermissionChecker, PermissionDecision

        return {
            "PermissionChecker": PermissionChecker,
            "PermissionDecision": PermissionDecision,
        }[name]
    if name == "PermissionMode":
        from researchx.permissions.modes import PermissionMode

        return PermissionMode
    raise AttributeError(name)
