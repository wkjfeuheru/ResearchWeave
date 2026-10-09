"""Credential redaction for configuration displays and Web output."""

from __future__ import annotations
import json
import os
from pathlib import Path
from researchx.config.settings import Settings

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
        for key in ("RESEARCHX_MEMORY_QDRANT_API_KEY", "RESEARCHX_MEMORY_EMBEDDING_API_KEY")
        if (value := os.environ.get(key, ""))
    }


def evaluation_credentials(cwd: str | Path | None = None) -> set[str]:
    """Langfuse credentials join output redaction without importing its SDK."""
    values = {
        value
        for key in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY")
        if (value := os.environ.get(key, ""))
    }
    path = Path(cwd or Path.cwd()) / ".researchx" / "evaluation.local.json"
    try:
        config = json.loads(path.read_text()).get("langfuse", {})
        values.update(
            config[key]
            for key in ("public_key", "secret_key")
            if isinstance(config.get(key), str) and config[key]
        )
    except (OSError, ValueError, AttributeError):
        pass
    return values
