"""API error types for ResearchX."""

from __future__ import annotations


class ResearchXApiError(RuntimeError):
    """Base class for upstream API failures."""


class AuthenticationFailure(ResearchXApiError):
    """Raised when the upstream service rejects the provided credentials."""


class RateLimitFailure(ResearchXApiError):
    """Raised when the upstream service rejects the request due to rate limits."""


class RequestFailure(ResearchXApiError):
    """Raised for generic request or transport failures."""
