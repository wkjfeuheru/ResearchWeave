"""Bounded, ordered SSE delivery with explicit detachment and backpressure."""

from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator, Awaitable, Callable

from anyio import CancelScope
from fastapi.responses import StreamingResponse
from starlette.types import Scope, Receive, Send


def encode_event(event: dict[str, object]) -> str:
    return "data: " + json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n\n"


class SessionEventResponse(StreamingResponse):
    """Also close on ASGI 2.4 send errors or disconnect before iterator startup."""

    def __init__(self, frames: AsyncIterator[str], close: Callable[[], Awaitable[None]]) -> None:
        super().__init__(
            frames,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
        )
        self.close_controller = close

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            with CancelScope(shield=True):
                await self.close_controller()


class EventChannel:
    def __init__(self, capacity: int = 128, heartbeat_seconds: float = 15.0) -> None:
        if capacity < 1 or heartbeat_seconds <= 0:
            raise ValueError("事件容量和心跳间隔必须大于零")
        self.queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=capacity)
        self.heartbeat_seconds = heartbeat_seconds
        self.detached = asyncio.Event()
        self.lock = asyncio.Lock()

    async def publish(self, event: dict[str, object]) -> None:
        # Freeze mutable message/progress projections before waiting for space.
        await self._put(encode_event(event))

    async def finish(self) -> None:
        """End gracefully after already queued events, including session_deleted."""
        await self._put(None)

    async def _put(self, value: str | None) -> None:
        async with self.lock:
            if self.detached.is_set():
                return
            put = asyncio.create_task(self.queue.put(value))
            closed = asyncio.create_task(self.detached.wait())
            try:
                await asyncio.wait((put, closed), return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in (put, closed):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(put, closed, return_exceptions=True)

    def detach(self) -> None:
        self.detached.set()

    async def stream(self) -> AsyncIterator[str]:
        while not self.detached.is_set():
            try:
                item = await asyncio.wait_for(self.queue.get(), self.heartbeat_seconds)
            except asyncio.TimeoutError:
                yield ": keep-alive\n\n"
                continue
            if item is None:
                return
            yield item
