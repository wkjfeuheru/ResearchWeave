"""Concurrent confirmations must all remain reachable and cancellable."""

import asyncio
from types import SimpleNamespace

import pytest

from openharness.web.runtime import BrowserConnection, Redactor
from openharness.engine.stream_events import ToolExecutionCompleted, ToolExecutionStarted


@pytest.fixture
def connection(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr("openharness.web.runtime.models_list", lambda: {"items": []})
    events = []

    async def send_json(data):
        events.append(data)

    connection = BrowserConnection(SimpleNamespace(send_json=send_json), "session", None)
    connection.request_id = "turn"
    connection.rows = []
    return connection, events


async def until(predicate):
    async with asyncio.timeout(1):
        while not predicate():
            await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_four_parallel_prompts_mixed_answers_and_stale_replies(connection):
    conn, events = connection
    tasks = [asyncio.create_task(conn.permission(f"mcp__finance__tool{i}", "confirm")) for i in range(4)]
    await until(lambda: len(conn.prompts) == 4)
    assert len(events) == 1
    first = events[0]["prompt_id"]
    conn.respond("stale-turn", first, "allow")
    conn.respond("turn", "stale-prompt", "allow")
    assert not tasks[0].done()
    for i, answer in enumerate(["allow", "deny", "allow", "deny"]):
        await until(lambda: len(events) == i + 1)
        prompt = events[i]
        assert prompt["tool_name"] == f"mcp__finance__tool{i}"
        assert "finance / tool" in prompt["tool_label"]
        conn.respond("turn", prompt["prompt_id"], answer)
        conn.respond("turn", prompt["prompt_id"], "allow")  # duplicate cannot authorize next tool
    assert await asyncio.gather(*tasks) == [True, False, True, False]
    assert not conn.prompts and not conn.prompt_lock.locked()


@pytest.mark.asyncio
async def test_cancel_clears_active_and_queued_confirmations_and_allows_next_turn(connection):
    conn, events = connection
    tasks = [asyncio.create_task(conn.permission(f"tool{i}", "confirm")) for i in range(4)]
    await until(lambda: len(conn.prompts) == 4)
    await conn.cancel()
    settled = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(isinstance(value, asyncio.CancelledError) for value in settled)
    assert len(events) == 1 and not conn.prompts and not conn.prompt_lock.locked()
    conn.request_id = "new-turn"
    task = asyncio.create_task(conn.permission("new-tool", "confirm"))
    await until(lambda: len(events) == 2)
    conn.respond("turn", events[0]["prompt_id"], "allow")
    assert not task.done()
    conn.respond("new-turn", events[1]["prompt_id"], "allow")
    assert await task is True


@pytest.mark.asyncio
async def test_activity_distinguishes_empty_from_failure_and_redacts_details(connection, monkeypatch):
    conn, events = connection
    monkeypatch.setenv("TAVILY_API_KEY", "secret-to-redact")
    conn.redactor = Redactor()
    await conn.event(ToolExecutionStarted("web_search", {"query": "solar"}, tool_use_id="search"))
    await conn.event(ToolExecutionCompleted("web_search", "", tool_use_id="search",
                                          metadata={"outcome": "empty", "detail": "no matches secret-to-redact"}))
    row = conn.rows[0]
    assert row["status"] == "completed" and row["outcome"] == "empty"
    assert "secret-to-redact" not in str(row) and "secret-to-redact" not in str(events)
