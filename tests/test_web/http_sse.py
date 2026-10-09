"""HTTPX SSE client for opt-in evaluations, without reconnect or replay."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager


class SSEConnection:
    def __init__(self, client, session_id, response, max_event_bytes):
        self.client = client
        self.session_id = session_id
        self.lines = response.aiter_lines()
        self.connection_id = ""
        self.max_event_bytes = max_event_bytes

    async def recv(self):
        async for line in self.lines:
            if not line.startswith("data: "):
                continue
            if len(line.encode("utf-8")) > self.max_event_bytes:
                raise ValueError("SSE event exceeds evaluation receive limit")
            data = line[6:]
            event = json.loads(data)
            if event["type"] == "ready":
                self.connection_id = event["connection_id"]
            return data
        raise EOFError("SSE disconnected; evaluation will not resend the request")

    async def send(self, payload):
        response = await self.client.post(
            f"/api/sessions/{self.session_id}/commands",
            json=json.loads(payload),
            headers={"X-ResearchX-Connection": self.connection_id},
        )
        response.raise_for_status()
        if response.status_code != 202 or response.json().get("accepted") is not True:
            raise ValueError("Invalid HTTP command acknowledgement")


@asynccontextmanager
async def sse_connection(client, session_id, *, max_event_bytes=4_000_000):
    async with client.stream("GET", f"/api/sessions/{session_id}/events", timeout=None) as response:
        response.raise_for_status()
        if not response.headers.get("content-type", "").startswith("text/event-stream"):
            raise ValueError("Expected text/event-stream")
        yield SSEConnection(client, session_id, response, max_event_bytes)
