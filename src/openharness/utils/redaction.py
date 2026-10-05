"""Credential redaction for configuration displays and Web output."""

from __future__ import annotations
import json
import os
from openharness.config.settings import Settings

_SECRET_KEY_PARTS = (
    "api_key",
    "apikey",
    "auth_token",
    "access_token",
    "refresh_token",
    "token",
    "secret",
    "password",
    "authorization",
    "credential",
    "private_key",
)

_REDACTED = "[REDACTED]"


def _is_secret_key(key: object) -> bool:
    normalized = str(key).lower().replace("-", "_")
    return any(part in normalized for part in _SECRET_KEY_PARTS)


def _redact_config_value(value: object, *, key: object | None = None) -> object:
    if key is not None and _is_secret_key(key):
        return _REDACTED
    if isinstance(value, dict):
        return {
            item_key: _redact_config_value(item_value, key=item_key)
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [_redact_config_value(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_config_value(item) for item in value]
    if isinstance(value, str) and value.lower().startswith(("bearer ", "basic ")):
        return _REDACTED
    return value


def _settings_json_for_display(settings: Settings) -> str:
    return json.dumps(_redact_config_value(settings.model_dump()), indent=2, default=str)


def memory_credentials() -> set[str]:
    """Also redact credentials left by retired memory integrations."""
    return {
        value
        for key in ("OPENHARNESS_MEMORY_QDRANT_API_KEY", "OPENHARNESS_MEMORY_EMBEDDING_API_KEY")
        if (value := os.environ.get(key, ""))
    }
