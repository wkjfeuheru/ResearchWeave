"""Conversation grants survive recovery without crossing policy or session boundaries."""

from tests.test_web.sse_client import sse_connect, RecordingChannel

from researchx.services.execution.async_timeout import timeout as async_timeout
import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from researchx.api.client import ApiMessageCompleteEvent
from researchx.api.usage import UsageSnapshot
from researchx.config import load_settings, save_settings
from researchx.config.settings import PathRuleConfig
from researchx.engine.messages import ConversationMessage, ToolUseBlock
from researchx.services.sessions.storage import _persistable_tool_metadata
from researchx.web.app import create_app
from researchx.web.runtime import SessionController
from tests.test_web.test_app import ORIGIN, FakeClient, add_model, add_session, collect, submit


def respond(socket, prompt, answer="allow_session"):
    socket.send_json(
        {
            "type": "response",
            "request_id": prompt["request_id"],
            "prompt_id": prompt["prompt_id"],
            "answer": answer,
        }
    )


def grant_write(socket):
    submit(socket, "write")
    permission = collect(socket, "prompt")[-1]
    assert permission["kind"] == "permission" and permission["session_scope"]
    respond(socket, permission)
    edit = collect(socket, "prompt")[-1]
    assert edit["kind"] == "edit" and "此文件" in edit["session_scope"]
    respond(socket, edit)
    assert not collect(socket)[-1]["failed"]


async def test_session_grants_survive_new_turn_model_switch_and_server_recovery(workspace):
    client, app, _, _, cwd = workspace
    profile = add_model(client)
    second_profile = add_model(client, label="Second model")
    sid = add_session(client, profile)
    with sse_connect(client, sid, headers=ORIGIN) as socket:
        socket.receive_json()
        grant_write(socket)
        submit(socket, "write", request_id="r2", profile_id=second_profile)
        events = collect(socket)
        assert not events[-1]["failed"] and not any(e["type"] == "prompt" for e in events)
    saved = (await app.state.workspace.record(sid))["tool_metadata"]["session_approvals"]
    assert saved == {"tools": ["write_file"], "edit_paths": [str(cwd / "note.txt")]}
    assert load_settings().permission.allowed_tools == []
    with TestClient(create_app(str(cwd)), base_url="http://localhost") as recovered:
        with sse_connect(recovered, sid, headers=ORIGIN) as socket:
            socket.receive_json()
            submit(socket, "write", request_id="restored")
            events = collect(socket)
            assert not events[-1]["failed"] and not any(e["type"] == "prompt" for e in events)
        other = add_session(recovered, profile)
        with sse_connect(recovered, other, headers=ORIGIN) as socket:
            socket.receive_json()
            submit(socket, "write")
            prompt = collect(socket, "prompt")[-1]
            assert prompt["kind"] == "permission"
            respond(socket, prompt, "deny")
            collect(socket)
        assert (
            "session_approvals" not in (await recovered.app.state.workspace.record(other))["tool_metadata"]
        )


class ActionClient(FakeClient):
    def __init__(self, action):
        super().__init__([])
        self.action = action

    async def stream_message(self, request):
        last = next(m for m in reversed(request.messages) if m.content)
        if last.text == "action":
            name, arguments = self.action
            message = ConversationMessage(
                role="assistant", content=[ToolUseBlock(name=name, input=arguments)]
            )
            yield ApiMessageCompleteEvent(message=message, usage=UsageSnapshot())
        else:
            async for event in super().stream_message(request):
                yield event


def test_grant_does_not_authorize_other_tools_or_other_file_reviews(workspace, monkeypatch):
    client, _, _, _, cwd = workspace
    sid = add_session(client, add_model(client))
    with sse_connect(client, sid, headers=ORIGIN) as socket:
        socket.receive_json()
        grant_write(socket)
        monkeypatch.setattr(
            "researchx.runtime._resolve_api_client_from_settings",
            lambda settings: ActionClient(("write_file", {"path": "other.txt", "content": "new"})),
        )
        submit(socket, "action", request_id="different-file")
        prompt = collect(socket, "prompt")[-1]
        assert prompt["kind"] == "edit" and prompt["path"] == str(cwd / "other.txt")
        respond(socket, prompt, "deny")
        collect(socket)
        assert not (cwd / "other.txt").exists()
        (cwd / "note.txt").write_text("original")
        monkeypatch.setattr(
            "researchx.runtime._resolve_api_client_from_settings", lambda settings: FakeClient([])
        )
        submit(socket, "edit", request_id="different-tool")
        prompt = collect(socket, "prompt")[-1]
        assert prompt["kind"] == "permission" and prompt["tool_name"] == "edit_file"
        respond(socket, prompt, "deny")
        collect(socket)
        assert (cwd / "note.txt").read_text() == "original"


async def test_one_off_permission_does_not_become_a_session_grant(workspace):
    client, app, _, _, _ = workspace
    sid = add_session(client, add_model(client))
    with sse_connect(client, sid, headers=ORIGIN) as socket:
        socket.receive_json()
        submit(socket, "write")
        respond(socket, collect(socket, "prompt")[-1], "allow")
        respond(socket, collect(socket, "prompt")[-1], "allow")
        collect(socket)
        submit(socket, "write", request_id="r2")
        prompt = collect(socket, "prompt")[-1]
        assert prompt["kind"] == "permission"
        respond(socket, prompt, "deny")
        collect(socket)
    assert "session_approvals" not in (await app.state.workspace.record(sid))["tool_metadata"]


@pytest.mark.parametrize(
    "policy", ["denied_tool", "denied_path", "sensitive_path", "denied_command"]
)
async def test_session_grants_cannot_bypass_permission_policy(workspace, monkeypatch, policy):
    client, app, _, _, cwd = workspace
    sid = add_session(client, add_model(client))
    target = cwd / "blocked.txt"
    settings = load_settings()
    if policy == "denied_tool":
        settings.permission.denied_tools = ["write_file"]
    elif policy == "denied_path":
        settings.permission.path_rules = [PathRuleConfig(pattern=str(target), allow=False)]
    elif policy == "sensitive_path":
        target = cwd / ".ssh" / "id_rsa"
    else:
        settings.permission.denied_commands = ["printf denied"]
    save_settings(settings)
    name, arguments = (
        ("bash", {"command": "printf denied"})
        if policy == "denied_command"
        else ("write_file", {"path": str(target), "content": "must not write"})
    )
    record = (await app.state.workspace.record(sid))
    record["tool_metadata"]["session_approvals"] = {"tools": [name], "edit_paths": [str(target)]}
    (await app.state.workspace.store.write(record))
    monkeypatch.setattr(
        "researchx.runtime._resolve_api_client_from_settings",
        lambda settings: ActionClient((name, arguments)),
    )
    with sse_connect(client, sid, headers=ORIGIN) as socket:
        socket.receive_json()
        submit(socket, "action")
        events = collect(socket)
        assert not any(e["type"] == "prompt" for e in events)
        assert any(
            e["type"] == "message" and e.get("message", {}).get("status") == "failed"
            for e in events
        )
    assert not target.exists()


@pytest.mark.asyncio
async def test_parallel_calls_reuse_grant_and_ignore_stale_responses(workspace):
    client, app, _, _, _ = workspace
    sid = add_session(client, add_model(client))
    events = []

    async def send_json(event):
        events.append(event)

    connection = SessionController(sid, app.state.workspace)
    connection.channel = RecordingChannel(events)
    connection.bundle = SimpleNamespace(engine=SimpleNamespace(tool_metadata={}))
    connection.request_id = "turn"
    tasks = [
        asyncio.create_task(connection.permission("mcp__finance__income", "confirm"))
        for _ in range(4)
    ]
    async with async_timeout(2):
        while len(connection.prompts) != 4:
            await asyncio.sleep(0)
    prompt = events[0]
    connection.respond("old-turn", prompt["prompt_id"], "allow_session")
    connection.respond("turn", "stale-prompt", "allow_session")
    assert "session_approvals" not in connection.bundle.engine.tool_metadata
    connection.respond("turn", prompt["prompt_id"], "allow_session")
    assert await asyncio.gather(*tasks) == [True] * 4
    assert len(events) == 1 and not connection.prompts
    assert (await app.state.workspace.record(sid))["tool_metadata"]["session_approvals"] == {
        "tools": ["mcp__finance__income"]
    }
    waiting = [
        asyncio.create_task(connection.permission("another-tool", "confirm")) for _ in range(2)
    ]
    async with async_timeout(2):
        while len(connection.prompts) != 2:
            await asyncio.sleep(0)
    pending = events[-1]
    await connection.cancel()
    connection.respond("turn", pending["prompt_id"], "allow_session")
    assert all(
        isinstance(value, asyncio.CancelledError)
        for value in await asyncio.gather(*waiting, return_exceptions=True)
    )
    assert connection.bundle.engine.tool_metadata["session_approvals"] == {
        "tools": ["mcp__finance__income"]
    }
    assert not connection.prompts and not connection.prompt_lock.locked()


@pytest.mark.asyncio
async def test_failed_grant_commit_does_not_authorize_operation(workspace, monkeypatch):
    client, app, _, _, _ = workspace
    sid = add_session(client, add_model(client))
    events = []

    async def send_json(event):
        events.append(event)

    connection = SessionController(sid, app.state.workspace)
    connection.channel = RecordingChannel(events)
    connection.bundle = SimpleNamespace(engine=SimpleNamespace(tool_metadata={}))
    connection.request_id = "turn"
    task = asyncio.create_task(connection.permission("bash", "confirm"))
    async with async_timeout(2):
        while not events:
            await asyncio.sleep(0)

    def fail_write(record):
        raise OSError("disk full")

    monkeypatch.setattr(app.state.workspace.store, "write", fail_write)
    connection.respond("turn", events[0]["prompt_id"], "allow_session")
    with pytest.raises(OSError, match="disk full"):
        await task
    assert "session_approvals" not in connection.bundle.engine.tool_metadata
    assert "session_approvals" not in (await app.state.workspace.record(sid))["tool_metadata"]
    assert not connection.prompts and not connection.prompt_lock.locked()


def test_corrupt_grants_do_not_turn_into_wildcard_authorization():
    assert _persistable_tool_metadata(
        {"session_approvals": {"tools": "bash", "edit_paths": True, "all": True}}
    ) == {"session_approvals": {}}
    assert _persistable_tool_metadata(
        {"session_approvals": {"tools": [False, "bash", "bash", ""]}}
    ) == {"session_approvals": {"tools": ["bash"]}}
