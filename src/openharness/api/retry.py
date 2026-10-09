"""Shared transport retry policy with transactional stream attempts."""

from __future__ import annotations

import asyncio
import random
import time
import json
import logging
from dataclasses import is_dataclass, fields
from pydantic import BaseModel
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from uuid import uuid4
from typing import Any, AsyncIterator, Callable

import httpx


MAX_BUFFER_BYTES = 16 * 1024 * 1024
MAX_BUFFER_EVENTS = 200000


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
    body = getattr(exc, "body", None)
    if not isinstance(body, dict):
        response = getattr(exc, "response", None)
        try:
            parse_body = getattr(response, "json", None)
            body = parse_body() if callable(parse_body) else {}
        except (ValueError, TypeError, RuntimeError, httpx.ResponseNotRead):
            body = {}
    error = body.get("error", body) if isinstance(body, dict) else {}
    codes = {str(getattr(exc, "code", "")).lower()}
    if isinstance(error, dict):
        codes.update(str(error.get(key, "")).lower() for key in ("code", "type"))
    if codes & {"authentication_error", "permission_error", "unauthorized", "invalid_api_key"}:
        return "authentication"
    if codes & {
        "insufficient_quota",
        "quota_exhausted",
        "billing_hard_limit_reached",
        "credit_balance_too_low",
    }:
        return "quota_exhausted"
    if codes & {"context_length_exceeded", "prompt_too_long", "max_tokens_exceeded"}:
        return "context_length"
    if codes & {"rate_limit_exceeded", "rate_limit_error", "rate_limited"}:
        return "rate_limit"
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
    if codes & {"invalid_request_error", "invalid_request", "invalid_argument"} or status in {
        400,
        404,
        405,
        409,
        422,
    }:
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
    value = (headers.get("retry-after") or headers.get("Retry-After")) if headers else None
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

    if not 1 <= max_attempts <= 4:
        raise ValueError("API attempts must be within 1..4")
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
        buffered: list[Any] = []
        final = None
        buffered_bytes = 0
        try:
            if before_attempt:
                before_attempt()
            stream = stream_once(request)
            try:
                async for event in stream:
                    if isinstance(event, ApiMessageCompleteEvent):
                        final = event
                    buffered_bytes += _event_bytes(event)
                    if len(buffered) >= MAX_BUFFER_EVENTS or buffered_bytes > MAX_BUFFER_BYTES:
                        raise ValueError("API attempt event limit exceeded")
                    buffered.append(event)
            finally:
                close = getattr(stream, "aclose", None)
                if close is not None:
                    await close()
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
                "usage_status": _usage_status(final),
                "usage": final.usage.model_dump()
                if final is not None and _usage_status(final) != "unknown"
                else None,
                "started": started,
                "finished": time.time(),
            }
            if isinstance(exc, asyncio.CancelledError):
                try:
                    _record(request, record)
                except Exception:
                    logging.getLogger(__name__).error(
                        "Cancellation audit could not settle; running attempt remains unknown"
                    )
                raise
            _record(request, record)
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
                "usage_status": _usage_status(final),
                "usage": final.usage.model_dump()
                if final is not None and _usage_status(final) != "unknown"
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


def _usage_status(final: Any) -> str:
    if final is None or final.usage.usage_reported is False:
        return "unknown"
    if final.usage.usage_reported is True:
        return "reported"
    return "estimated" if final.usage.total_tokens else "unknown"


def _event_bytes(event: Any) -> int:
    """Measure the entire serializable event, including tool blocks and images."""

    def encode(value: Any) -> Any:
        if isinstance(value, BaseModel):
            return value.model_dump(mode="json")
        if is_dataclass(value) and not isinstance(value, type):
            return {field.name: getattr(value, field.name) for field in fields(value)}
        raise TypeError(f"Unsupported buffered event: {type(value).__name__}")

    return len(json.dumps(event, default=encode, ensure_ascii=False).encode("utf-8"))
