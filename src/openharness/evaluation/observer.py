"""Task-local observations with safe optional export and provider-neutral usage."""

from __future__ import annotations
from typing import TypeVar, overload, Callable, Iterable, Iterator, Mapping, AsyncIterator, cast
from openharness.api.client import SupportsStreamingMessages, ApiMessageRequest, ApiStreamEvent
from openharness.evaluation.models import Budget

import asyncio
import hashlib
import json
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from uuid import uuid4

from openharness.api.client import ApiMessageCompleteEvent, ApiRetryEvent
from openharness.evaluation.models import Observation
from openharness.engine.messages import ToolResultBlock

CleanT = TypeVar("CleanT")

_active: ContextVar[str | None] = ContextVar("evaluation_parent_observation", default=None)


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def fingerprint(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()


class EvaluationBudgetExceeded(RuntimeError):
    pass


class ObservationHandle:
    def __init__(self, observation: Observation, clean: Callable[[object], object]) -> None:
        self.observation, self.clean = observation, clean

    def update(self, **values: object) -> None:
        for key, value in values.items():
            if hasattr(self.observation, key):
                setattr(self.observation, key, self.clean(value))


class RecordingObserver:
    def __init__(self, *, secrets: Iterable[str] = (), budget: Budget | None = None) -> None:
        self.observations: list[Observation] = []
        self.blobs: dict[str, object] = {}
        self.secrets = sorted({s for s in secrets if s}, key=len, reverse=True)
        self.budget = budget
        self.trace_id: str | None = None
        self.export_failed = False

    @overload
    def clean(self, value: str) -> str: ...
    @overload
    def clean(self, value: dict[str, CleanT]) -> dict[str, CleanT]: ...
    @overload
    def clean(self, value: object) -> object: ...

    def clean(self, value: object) -> object:
        if isinstance(value, str):
            for secret in self.secrets:
                value = value.replace(secret, "[REDACTED]")
            return value
        if isinstance(value, dict):
            return {
                str(k): "[REDACTED]"
                if any(
                    term in str(k).lower()
                    for term in (
                        "api_key",
                        "api-key",
                        "apikey",
                        "authorization",
                        "auth_token",
                        "access_token",
                        "refresh_token",
                        "secret_key",
                        "password",
                        "private_key",
                        "credential",
                    )
                )
                else self.clean(v)
                for k, v in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [self.clean(v) for v in value]
        return value

    @contextmanager
    def span(
        self,
        name: str,
        *,
        kind: str = "span",
        input: object = None,
        metadata: Mapping[str, object] | None = None,
    ) -> Iterator[ObservationHandle]:
        if self.budget:
            if (
                kind == "generation"
                and sum(o.kind == kind for o in self.observations) >= self.budget.model_calls
            ):
                raise EvaluationBudgetExceeded("模型调用达到评测预算")
            if (
                kind == "tool"
                and sum(o.kind == kind for o in self.observations) >= self.budget.tool_calls
            ):
                raise EvaluationBudgetExceeded("工具调用达到评测预算")
        parent = _active.get()
        observation = Observation(
            id=uuid4().hex,
            parent_id=parent,
            name=name,
            kind=kind,
            started_at=timestamp(),
            input=self.clean(input),
            metadata=cast(dict[str, object], self.clean(metadata or {})),
        )
        self.observations.append(observation)
        token = _active.set(observation.id)
        start = time.monotonic()
        try:
            yield ObservationHandle(observation, self.clean)
            if observation.status == "running":
                observation.status = "ok"
        except (asyncio.CancelledError, GeneratorExit):
            observation.status = "cancelled"
            raise
        except BaseException:
            observation.status = "error"
            raise
        finally:
            observation.duration_ms = (time.monotonic() - start) * 1000
            _active.reset(token)

    def total_tokens(self) -> int:
        return sum(
            (o.usage or {}).get("input_tokens", 0) + (o.usage or {}).get("output_tokens", 0)
            for o in self.observations
            if o.kind == "generation"
        )

    def store_blob(self, value: object) -> dict[str, str]:
        clean = self.clean(value)
        key = fingerprint(clean)
        self.blobs.setdefault(key, clean)
        return {"$blob": key}


class ObservedClient:
    """One wrapper observes main, compaction, hook and nested investigation calls."""

    def __init__(self, client: SupportsStreamingMessages, observer: RecordingObserver) -> None:
        self.client, self.observer = client, observer

    def __getattr__(self, name: str) -> object:
        return getattr(self.client, name)

    async def stream_message(self, request: ApiMessageRequest) -> AsyncIterator[ApiStreamEvent]:
        payload = {
            "model": request.model,
            "system": request.system_prompt,
            "messages": [
                self.observer.store_blob(
                    {
                        "role": m.role,
                        "text": m.text,
                        "runtime_context": m.runtime_context,
                        "tool_calls": [{"name": t.name, "input": t.input} for t in m.tool_uses],
                        "tool_results": [
                            {"id": b.tool_use_id, "text": b.content, "is_error": b.is_error}
                            for b in m.content
                            if isinstance(b, ToolResultBlock)
                        ],
                    }
                )
                for m in request.messages
            ],
            "tool_schema_hash": fingerprint(request.tools),
        }
        with self.observer.span(
            "model",
            kind="generation",
            input=payload,
            metadata={
                "model": request.model,
                "prompt_hash": fingerprint(request.system_prompt),
                "effort": request.effort,
            },
        ) as span:
            retries = 0
            complete = False
            async for event in self.client.stream_message(request):
                if isinstance(event, ApiRetryEvent):
                    retries += 1
                    span.update(metadata={**span.observation.metadata, "retries": retries})
                if isinstance(event, ApiMessageCompleteEvent):
                    complete = True
                    usage = event.usage.model_dump()
                    reported = getattr(event.usage, "usage_reported", None)
                    usage["reported"] = (
                        reported
                        if reported is not None
                        else bool(event.usage.input_tokens or event.usage.output_tokens)
                    )
                    span.update(
                        output={
                            "text": event.message.text,
                            "tool_calls": [
                                {"name": t.name, "input": t.input} for t in event.message.tool_uses
                            ],
                        },
                        usage=usage,
                    )
                yield event
            if not complete:
                span.update(
                    status="error",
                    metadata={**span.observation.metadata, "incomplete_stream": True},
                )
            if (
                self.observer.budget
                and self.observer.total_tokens() > self.observer.budget.total_tokens
            ):
                raise EvaluationBudgetExceeded("Token 消耗达到评测预算")

    async def close(self) -> None:
        close = getattr(self.client, "close", None)
        if close:
            await close()
