"""Errors shared by research storage and its conflict coordinator."""


class ResearchError(ValueError):
    """A research record cannot be read or committed safely."""
