"""Public process projections, tool correlation, and destructive session cleanup."""

from tests.test_web.sse_client import sse_connect, RecordingChannel

import json

import pytest
from fastapi.testclient import TestClient
import httpx

from researchx.api.client import ApiMessageCompleteEvent, ApiTextDeltaEvent
from researchx.api.usage import UsageSnapshot
from researchx.engine.messages import ConversationMessage, TextBlock, ToolUseBlock
from researchx.engine.stream_events import (
    AssistantTextDelta,
    AssistantTurnComplete,
    ToolExecutionCompleted,
    ToolExecutionStarted,
)
from researchx.state.store import ResearchStore
from researchx.web.activity import describe_tool
from researchx.web.app import create_app
from researchx.web.runtime import SessionController
from tests.test_web.test_app import ORIGIN, SECRET, add_model, add_session, collect, submit


class RecordingSocket:
    def __init__(self):
        self.events = []

    async def send_json(self, event):
        # Preserve the payload at send time instead of references to mutable rows.
        self.events.append(json.loads(json.dumps(event)))


@pytest.mark.asyncio
async def test_process_ids_parallel_same_tool_and_redaction(workspace):
    _, app, _, _, _ = workspace
    add_model(workspace[0])
    socket = RecordingSocket()
    connection = SessionController("abcdef123456", app.state.workspace)
    connection.channel = RecordingChannel(socket.events)
    connection.request_id = "turn"
    connection.rows = []
    await connection.event(AssistantTextDelta("先读取两份资料。"))
    await connection.event(
        AssistantTurnComplete(
            ConversationMessage(
                role="assistant",
                content=[
                    TextBlock(text="先读取两份资料。"),
                    ToolUseBlock(id="a", name="read_file", input={}),
                ],
            ),
            UsageSnapshot(),
        )
    )
    await connection.event(
        ToolExecutionStarted("read_file", {"path": f"{SECRET}.txt", "extra": "private"}, "a")
    )
    await connection.event(ToolExecutionStarted("read_file", {"path": "second.txt"}, "b"))
    await connection.event(ToolExecutionCompleted("read_file", "raw result", True, tool_use_id="b"))
    assert connection.rows[-2]["status"] == "running"
    assert connection.rows[-1]["status"] == "failed"
    await connection.event(ToolExecutionCompleted("read_file", "raw result", tool_use_id="a"))
    assert connection.rows[-2]["status"] == "completed"
    assert connection.rows[0]["phase"] == "progress"
    assert connection.rows[0]["id"] == socket.events[0]["id"]
    public = json.dumps(socket.events)
    assert SECRET not in public and "raw result" not in public and "private" not in public
    assert all(row["turn_id"] == "turn" for row in connection.rows)


def test_process_projection_preserves_url_path_but_discards_credentials():
    description = describe_tool(
        "web_fetch", {"url": "https://user:password@example.com/report?token=secret#private"}
    )
    assert description["target"] == "https://example.com/report"
    assert describe_tool("unknown", {"operation": {"method": "private"}}) == {
        "category": "other",
        "label": "执行工具操作",
        "target": "",
    }


def test_crashed_running_projection_restores_as_stopped_history(workspace):
    client, app, _, _, cwd = workspace
    sid = add_session(client, add_model(client))
    record = app.state.workspace.store.load_by_id(cwd, sid)
    record["display_messages"] = [
        {
            "id": "operation",
            "role": "activity",
            "text": "",
            "turn_id": "old",
            "turn_status": "running",
            "status": "running",
            "category": "read",
            "label": "读取文件",
        },
        {
            "id": "partial",
            "role": "assistant",
            "text": "部分文字",
            "turn_id": "old",
            "turn_status": "running",
            "phase": "pending",
        },
    ]
    app.state.workspace.store.write(record)
    rows = client.get(f"/api/sessions/{sid}").json()["messages"]
    assert all(row["turn_status"] == "stopped" for row in rows)
    assert rows[0]["status"] == "interrupted" and rows[1]["phase"] == "progress"
    assert client.get(f"/api/sessions/{sid}").json()["messages"] == rows


def test_process_completion_persistence_restart_and_stop(workspace):
    client, app, _, _, cwd = workspace
    sid = add_session(client, add_model(client))
    with sse_connect(client, sid, headers=ORIGIN) as socket:
        socket.receive_json()
        submit(socket, "ask")
        events = collect(socket, "prompt")
        operation = next(e["message"] for e in events if e["type"] == "message")
        assert operation["role"] == "activity" and operation["status"] == "running"
        socket.send_json(
            {
                "type": "response",
                "request_id": "r1",
                "prompt_id": events[-1]["prompt_id"],
                "answer": "一年",
            }
        )
        completed = collect(socket)[-1]["session"]["messages"]
        assert (
            next(row for row in completed if row["id"] == operation["id"])["status"] == "completed"
        )
        assert completed[-1]["phase"] == "final"
        assert all(row["turn_status"] == "completed" for row in completed)
        submit(socket, "ask", request_id="r2")
        collect(socket, "prompt")
        socket.send_json({"type": "cancel", "request_id": "r2"})
        stopped = collect(socket)[-1]["session"]["messages"]
        last_operation = next(row for row in reversed(stopped) if row["role"] == "activity")
        assert (
            last_operation["status"] == "interrupted" and last_operation["turn_status"] == "stopped"
        )
    with TestClient(create_app(str(cwd)), base_url="http://localhost") as restarted:
        assert restarted.get(f"/api/sessions/{sid}").json()["messages"] == stopped
    assert app.state.workspace.store.load_by_id(cwd, sid)["display_messages"] == stopped


def test_delete_session_removes_only_owned_data_and_closes_idle_socket(workspace):
    client, app, _, _, cwd = workspace
    profile = add_model(client)
    sid, other = add_session(client, profile), add_session(client, profile)
    original = cwd / "report.txt"
    original.write_text("original")
    research = ResearchStore(cwd, sid)
    research.capture(origin_id="source", content="snapshot")
    other_research = ResearchStore(cwd, other)
    other_research.capture(origin_id="other", content="other snapshot")
    with sse_connect(client, sid, headers=ORIGIN) as socket:
        socket.receive_json()
        response = client.delete(f"/api/sessions/{sid}")
        assert response.status_code == 200 and response.json() == {"ok": True}
        assert socket.receive_json()["type"] == "session_deleted"
        with pytest.raises(EOFError):
            socket.receive_json()
    assert not app.state.workspace.store._path(sid).exists()
    assert not research.directory.exists()
    assert original.read_text() == "original"
    assert other_research.directory.exists()
    assert [item["session_id"] for item in client.get("/api/sessions").json()["items"]] == [other]
    assert client.get(f"/api/sessions/{sid}").status_code == 404
    assert client.delete(f"/api/sessions/{sid}").status_code == 404
    assert client.delete("/api/sessions/invalid").status_code == 404
    with (
        pytest.raises(httpx.HTTPStatusError),
        sse_connect(client, sid, headers=ORIGIN),
    ):
        pass
    assert not research.directory.exists()


def test_delete_running_and_approval_sessions_rejected_then_allowed(workspace):
    client, app, _, _, _ = workspace
    sid = add_session(client, add_model(client))
    with sse_connect(client, sid, headers=ORIGIN) as socket:
        socket.receive_json()
        for request_id, text, terminal in [("r1", "slow", "delta"), ("r2", "ask", "prompt")]:
            submit(socket, text, request_id=request_id)
            collect(socket, terminal)
            assert client.delete(f"/api/sessions/{sid}").status_code == 409
            assert app.state.workspace.store._path(sid).exists()
            socket.send_json({"type": "cancel", "request_id": request_id})
            collect(socket)
        assert client.delete(f"/api/sessions/{sid}").status_code == 200
        assert socket.receive_json()["type"] == "session_deleted"


def test_delete_storage_failure_preserves_record_and_allows_retry(workspace, monkeypatch):
    client, app, _, _, cwd = workspace
    sid = add_session(client, add_model(client))
    research = ResearchStore(cwd, sid)
    research.capture(origin_id="source", content="snapshot")
    import shutil

    real_remove = shutil.rmtree

    def failing_remove(path):
        raise PermissionError("private path must not leak")

    with monkeypatch.context() as context:
        context.setattr("researchx.web.storage.shutil.rmtree", failing_remove)
        response = client.delete(f"/api/sessions/{sid}")
        assert response.status_code == 500 and "private path" not in response.text
        assert client.get(f"/api/sessions/{sid}").status_code == 200
        assert sid not in app.state.workspace.deleting
    assert shutil.rmtree is real_remove
    assert client.delete(f"/api/sessions/{sid}").status_code == 200


def test_delete_corrupt_research_does_not_require_loading_it(workspace):
    client, _, _, _, cwd = workspace
    sid = add_session(client, add_model(client))
    research = ResearchStore(cwd, sid)
    research.directory.mkdir(parents=True, exist_ok=True)
    research.path.write_text("broken")
    assert client.get(f"/api/sessions/{sid}").status_code == 409
    assert client.delete(f"/api/sessions/{sid}").status_code == 200
    assert not research.directory.exists()


def test_failure_keeps_progress_without_promoting_it_to_a_final_answer(workspace, monkeypatch):
    client, _, _, _, _ = workspace
    sid = add_session(client, add_model(client))

    class FailingModel:
        async def close(self):
            pass

        async def stream_message(self, request):
            if any(block.type == "tool_result" for block in request.messages[-1].content):
                raise RuntimeError(f"private failure {SECRET}")
            yield ApiTextDeltaEvent("先检查报告。")
            yield ApiMessageCompleteEvent(
                message=ConversationMessage(
                    role="assistant",
                    content=[
                        TextBlock(text="先检查报告。"),
                        ToolUseBlock(id="check", name="read_file", input={"path": "missing.txt"}),
                    ],
                ),
                usage=UsageSnapshot(),
            )

    monkeypatch.setattr(
        "researchx.runtime._resolve_api_client_from_settings", lambda settings: FailingModel()
    )
    with sse_connect(client, sid, headers=ORIGIN) as socket:
        socket.receive_json()
        submit(socket, "检查报告")
        events = collect(socket)
    assert events[-1]["failed"]
    rows = events[-1]["session"]["messages"]
    assert all(row["turn_status"] == "failed" for row in rows)
    assert next(row for row in rows if row["role"] == "assistant")["phase"] == "progress"
    assert next(row for row in rows if row["role"] == "activity")["status"] == "failed"
    assert SECRET not in json.dumps(events)
