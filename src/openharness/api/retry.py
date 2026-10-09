"""Shared transport retry policy with transactional stream attempts."""

from __future__ import annotations

import asyncio
import random
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from uuid import uuid4
from typing import Any, AsyncIterator, Callable

import httpx


def classify_error(exc: BaseException) -> str:
    from openharness.api.errors import AuthenticationFailure, RateLimitFailure

    if isinstance(exc, asyncio.CancelledError):
        return "cancelled"
    text = str(exc).lower()
    status = getattr(exc, "status_code", None) or getattr(
        getattr(exc, "response", None), "status_code", None
    )
    if isinstance(exc, AuthenticationFailure) or status in {401, 403}:
        return "authentication"
    if any(
        token in text
        for token in (
            "insufficient_quota",
            "quota exhausted",
            "exceeded your current quota",
            "billing_hard_limit",
            "credit balance",
        )
    ):
        return "quota_exhausted"
    if any(
        token in text
        for token in ("context_length", "prompt too long", "maximum context", "input tokens exceed")
    ):
        return "context_length"
    if status in {400, 404, 405, 409, 422}:
        return "invalid_request"
    if status == 429 or isinstance(exc, RateLimitFailure):
        return "rate_limit"
    if status in {408, 500, 502, 503, 504, 529}:
        return "unavailable"
    if isinstance(
        exc, (httpx.TransportError, ConnectionError, TimeoutError, asyncio.TimeoutError, OSError)
    ) or type(exc).__name__ in {"APIConnectionError", "APITimeoutError"}:
        return "transport"
    return "unknown"


def retry_delay(attempt: int, exc: BaseException | None = None) -> float:
    headers = getattr(getattr(exc, "response", None), "headers", None) or getattr(
        exc, "headers", {}
    )
    value = headers.get("retry-after") if headers else None
    if value:
        try:
            return max(0.0, min(float(value), 30.0))
        except (TypeError, ValueError):
            try:
                return max(
                    0.0,
                    min(
                        (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds(),
                        30.0,
                    ),
                )
            except (TypeError, ValueError):
                pass
    delay = min(2.0**attempt, 30.0)
    return min(30.0, delay + random.uniform(0, delay * 0.25))


async def stream_with_retry(
    stream_once: Callable[..., Any],
    request: Any,
    *,
    translate: Callable[[Exception], Exception],
    max_attempts: int = 4,
    before_attempt: Callable[[], None] | None = None,
) -> AsyncIterator[Any]:
    from openharness.api.client import ApiMessageCompleteEvent, ApiRetryEvent

    request_id = uuid4().hex
    for number in range(1, max_attempts + 1):
        attempt_id = uuid4().hex
        started = time.time()
        from openharness.config.paths import get_data_dir
        from openharness.services.operations import OperationStore

        OperationStore(get_data_dir() / "executions" / "operations.sqlite3").record_api_attempt(
            {
                "request_id": request_id,
                "attempt_id": attempt_id,
                "attempt": number,
                "status": "running",
                "usage_status": "unknown",
                "usage": None,
                "started": started,
            }
        )
        buffered = []
        final = None
        buffered_bytes = 0
        try:
            if before_attempt:
                before_attempt()
            async for event in stream_once(request):
                buffered.append(event)
                buffered_bytes += len(getattr(event, "text", "").encode())
                if isinstance(event, ApiMessageCompleteEvent):
                    final = event
                if len(buffered) > 200000 or buffered_bytes > 16 * 1024 * 1024:
                    raise ValueError("API attempt event limit exceeded")
            if final is None:
                raise ConnectionError("Stream ended without a complete response")
        except BaseException as exc:
            category = classify_error(exc)
            retry = category in {"transport", "rate_limit", "unavailable"} and number < max_attempts
            delay = retry_delay(number - 1, exc) if retry else 0.0
            record = {
                "request_id": request_id,
                "attempt_id": attempt_id,
                "attempt": number,
                "status": "cancelled" if category == "cancelled" else "failed",
                "error_category": category,
                "wait_seconds": delay,
                "usage_status": "reported"
                if final and final.usage.usage_reported is not False
                else "unknown",
                "usage": final.usage.model_dump()
                if final and final.usage.usage_reported is not False
                else None,
                "started": started,
                "finished": time.time(),
            }
            _record(request, record)
            if isinstance(exc, asyncio.CancelledError):
                raise
            if not isinstance(exc, Exception):
                raise
            if not retry:
                raise translate(exc) from exc
            # No deltas from this failed attempt have left the buffer.
            yield ApiRetryEvent(
                message=category,
                attempt=number,
                max_attempts=max_attempts,
                delay_seconds=delay,
                request_id=request_id,
                attempt_id=attempt_id,
                error_category=category,
                usage_status=str(record["usage_status"]),
            )
            await asyncio.sleep(delay)
            continue
        _record(
            request,
            {
                "request_id": request_id,
                "attempt_id": attempt_id,
                "attempt": number,
                "status": "succeeded",
                "error_category": None,
                "wait_seconds": 0.0,
                "usage_status": "reported"
                if final.usage.usage_reported is not False
                else "unknown",
                "usage": final.usage.model_dump()
                if final.usage.usage_reported is not False
                else None,
                "started": started,
                "finished": time.time(),
            },
        )
        for event in buffered:
            yield event
        return


def _record(request: Any, record: dict[str, Any]) -> None:
    from openharness.config.paths import get_data_dir
    from openharness.services.operations import OperationStore

    OperationStore(get_data_dir() / "executions" / "operations.sqlite3").record_api_attempt(record)
    callback = getattr(request, "attempt_callback", None)
    if callback:
        callback(record)
