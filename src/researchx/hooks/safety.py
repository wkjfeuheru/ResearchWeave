"""Bounded, redacted hook payloads and explicitly trusted HTTP destinations."""

from __future__ import annotations

import json
import re
from typing import Any
from researchx.hooks.schemas import HttpHookDefinition
from urllib.parse import urlparse
import httpx
from researchx.security.network_guard import (
    validate_http_url,
    pinned_public_http_url,
    NetworkGuardError,
)

SENSITIVE = re.compile(
    r"authorization|cookie|token|secret|password|api.?key|credential|environment|^env$", re.I
)
SAFE_ENV = frozenset(
    {"PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR", "TEMP", "TMP", "SYSTEMROOT", "WINDIR"}
)


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if SENSITIVE.search(str(key)) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        if value.lstrip().startswith(("{", "[")):
            try:
                parsed = json.loads(value)
            except (ValueError, TypeError):
                pass
            else:
                return json.dumps(redact(parsed), ensure_ascii=False)
        value = re.sub(r'(?i)(bearer\s+)[^\s"\']+', r"\1[REDACTED]", value)
        return re.sub(
            r'(?i)((?:api[_-]?key|token|password|secret)\s*[=:]\s*)[^\s,;"\']+',
            r"\1[REDACTED]",
            value,
        )
    return value


async def post_hook(
    hook: HttpHookDefinition, event: str, payload: dict[str, Any]
) -> tuple[int, str]:
    validate_http_url(hook.url)
    parsed = urlparse(hook.url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    destination = hook.url
    hostname = parsed.hostname or ""
    if origin not in hook.trusted_origins:
        destination, hostname = await pinned_public_http_url(hook.url)
    body = json.dumps({"event": event, "payload": redact(payload)}).encode()
    if len(body) > hook.max_payload_bytes:
        raise ValueError("Hook request exceeds payload limit")
    # Never forward POST data/headers to any redirect. Internal origins require exact host configuration.
    async with httpx.AsyncClient(
        timeout=hook.timeout_seconds, follow_redirects=False, trust_env=False
    ) as client:
        async with client.stream(
            "POST",
            destination,
            content=body,
            extensions={"sni_hostname": hostname},
            headers={"Content-Type": "application/json", **hook.headers, "Host": parsed.netloc},
        ) as response:
            if response.has_redirect_location:
                raise NetworkGuardError("HTTP hook redirects are forbidden")
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > hook.max_output_bytes:
                    raise ValueError("Hook response exceeds output limit")
            return response.status_code, str(redact(data.decode("utf-8", errors="replace")))
