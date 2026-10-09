"""Credential discovery and streaming redaction for browser-visible payloads."""

from __future__ import annotations

from contextlib import suppress
from typing import TypeVar, overload
from fastapi import HTTPException
from researchx.web.catalog import models_list, profile_settings

RedactedT = TypeVar("RedactedT")


class Redactor:
    """Remove configured credentials from every browser-visible payload."""

    def __init__(self) -> None:
        from researchx.security.redaction import memory_credentials, evaluation_credentials

        from researchx.api.tavily_search import tavily_credentials

        self.secrets: set[str] = (
            memory_credentials() | tavily_credentials() | evaluation_credentials()
        )
        for profile in models_list()["items"]:
            if profile["supported"] and profile["configured"]:
                with suppress(ValueError, HTTPException):
                    self.secrets.add(profile_settings(profile["id"]).resolve_auth().value)

    @overload
    def clean(self, value: str) -> str: ...
    @overload
    def clean(self, value: dict[str, RedactedT]) -> dict[str, RedactedT]: ...
    @overload
    def clean(self, value: list[RedactedT]) -> list[RedactedT]: ...
    @overload
    def clean(self, value: object) -> object: ...

    def clean(self, value: object) -> object:
        if isinstance(value, str):
            for secret in sorted(self.secrets, key=len, reverse=True):
                if secret:
                    value = value.replace(secret, "[已隐藏凭据]")
            return value
        if isinstance(value, list):
            return [self.clean(v) for v in value]
        if isinstance(value, dict):
            return {k: self.clean(v) for k, v in value.items()}
        return value


class StreamingRedactor:
    """Hold credential prefixes so keys split across chunks never reach the UI."""

    def __init__(self, redactor: Redactor) -> None:
        self.redactor = redactor
        self.pending = ""

    def push(self, text: str) -> str:
        cleaned = self.redactor.clean(self.pending + text)
        hold = 0
        for secret in self.redactor.secrets:
            for size in range(1, min(len(secret), len(cleaned) + 1)):
                if cleaned.endswith(secret[:size]):
                    hold = max(hold, size)
        self.pending = cleaned[-hold:] if hold else ""
        return cleaned[:-hold] if hold else cleaned

    def flush(self) -> str:
        result = self.redactor.clean(self.pending)
        self.pending = ""
        return result
