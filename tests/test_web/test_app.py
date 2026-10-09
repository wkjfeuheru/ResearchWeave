"""End-to-end web transport tests using the real Agent loop and a fake model."""

from __future__ import annotations

import asyncio
import json

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from openharness.api.client import ApiMessageCompleteEvent, ApiTextDeltaEvent
from openharness.api.errors import AuthenticationFailure
from openharness.api.usage import UsageSnapshot
from openharness.config import Settings, save_settings
from openharness.engine.messages import ConversationMessage, TextBlock, ToolUseBlock
from openharness.web.app import create_app

SECRET = "test-secret-not-for-browser"
ORIGIN = {"origin": "http://localhost", "host": "localhost"}


class FakeClient:
    def __init__(self, requests):
        self.requests = requests
        self.closed = False

    async def close(self):
        self.closed = True

    async def stream_message(self, request):
        self.requests.append(request)
        last = next(message for message in reversed(request.messages) if message.content)
        if last.text == "fail":
            raise AuthenticationFailure(f"bad key: {SECRET}")
        if last.text == "slow":
            yield ApiTextDeltaEvent("部分回复")
            await asyncio.sleep(3600)
        if last.text in {"write", "edit", "ask", "skill-transition"}:
            names = {
                "write": "write_file",
                "edit": "edit_file",
                "ask": "ask_user_question",
                "skill-transition": "ask_user_question",
            }
            arguments = {
                "write": {"path": "note.txt", "content": "approved"},
                "edit": {"path": "note.txt", "old_str": "original", "new_str": "changed"},
                "ask": {"question": "你希望研究哪个时间范围？"},
                "skill-transition": {"question": "继续加载当前技能？"},
            }
            message = ConversationMessage(
                role="assistant",
                content=[
                    ToolUseBlock(name=names[last.text], input=arguments[last.text]),
                ],
            )
        elif any(
            str(getattr(block, "content", "")).startswith("invoke-skill") for block in last.content
        ):
            message = ConversationMessage(
                role="assistant",
                content=[
                    ToolUseBlock(name="skill", input={"name": "financial-statement-analysis"}),
                ],
            )
        else:
            text = f"已收到：{last.text}" if last.text else "工具处理完成"
            if last.text == "secret":
                text = f"不可显示 {SECRET}。"
            for chunk in (text[:12], text[12:24], text[24:]):
                if chunk:
                    yield ApiTextDeltaEvent(chunk)
            message = ConversationMessage(role="assistant", content=[TextBlock(text=text)])
        yield ApiMessageCompleteEvent(
            message=message, usage=UsageSnapshot(input_tokens=10, output_tokens=5)
        )


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))
    for name in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENHARNESS_OPENAI_API_KEY",
        "OPENHARNESS_ANTHROPIC_API_KEY",
        "OPENHARNESS_PROFILE",
        "ANTHROPIC_AUTH_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    save_settings(Settings(memory={"enabled": False}))
    requests = []
    clients = []

    def factory(settings):
        client = FakeClient(requests)
        clients.append(client)
        return client

    monkeypatch.setattr("openharness.runtime._resolve_api_client_from_settings", factory)
    monkeypatch.setattr("openharness.web.app._resolve_api_client_from_settings", factory)
    cwd = tmp_path / "research"
    cwd.mkdir()
    app = create_app(str(cwd))
    with TestClient(app, base_url="http://localhost") as client:
        yield client, app, requests, clients, cwd


def add_model(client, key=SECRET, label="Test model"):
    response = client.post(
        "/api/models",
        json={
            "label": label,
            "api_format": "openai",
            "model": "test-model",
            "context_window_tokens": 200_000,
            "base_url": "https://example.com/v1",
            "api_key": key,
        },
    )
    assert response.status_code == 201
    return response.json()["id"]


def add_session(client, profile):
    response = client.post("/api/sessions", json={"profile_id": profile})
    assert response.status_code == 201
    return response.json()["session_id"]


def collect(socket, until="done"):
    events = []
    for _ in range(100):
        event = socket.receive_json()
        events.append(event)
        if event["type"] in ((until,) if isinstance(until, str) else until):
            return events
    raise AssertionError("Missing terminal event")


def submit(socket, text, request_id="r1", **fields):
    socket.send_json({"type": "submit", "request_id": request_id, "text": text, **fields})


def test_profile_lifecycle_credential_retention_and_protection(workspace):
    client, _, _, _, _ = workspace
    profile = add_model(client)
    assert SECRET not in client.get("/api/models").text
    assert (
        client.put(
            f"/api/models/{profile}",
            json={
                "label": "Edited",
                "api_format": "openai",
                "model": "changed-model",
                "api_key": "",
            },
        ).status_code
        == 200
    )
    from openharness.web.catalog import profile_settings

    assert profile_settings(profile).resolve_auth().value == SECRET
    assert client.post(f"/api/models/{profile}/activate").status_code == 200
    assert client.delete(f"/api/models/{profile}").status_code == 409
    assert client.delete(f"/api/models/{profile}/credential").status_code == 200
    with pytest.raises(ValueError):
        profile_settings(profile).resolve_auth()
    assert client.post("/api/models/claude-api/activate").status_code == 200
    assert client.delete(f"/api/models/{profile}").status_code == 200
    assert client.get("/api/models").json()["active_profile"] == "claude-api"


def test_validation_and_local_access_do_not_echo_secrets(workspace):
    client, _, _, _, _ = workspace
    response = client.post("/api/models", json={"label": "", "api_key": SECRET})
    assert response.status_code == 422
    assert SECRET not in response.text
    assert client.get("/api/health", headers={"host": "attacker.example"}).status_code == 403
    assert (
        client.post(
            "/api/sessions", json={}, headers={"origin": "https://evil.example"}
        ).status_code
        == 403
    )
    for url in (
        "file:///etc/passwd",
        "https://key:secret@example.com",
        "https://example.com?key=secret",
    ):
        assert (
            client.post(
                "/api/models",
                json={
                    "label": "bad",
                    "api_format": "openai",
                    "model": "test",
                    "base_url": url,
                },
            ).status_code
            == 422
        )
    assert client.get("/api/sessions/../../settings.json").status_code == 404


def test_probe_has_no_session_side_effects(workspace):
    client, _, requests, clients, _ = workspace
    profile = add_model(client)
    assert client.post(f"/api/models/{profile}/test").json()["ok"]
    assert requests[-1].max_tokens == 64
    assert clients[-1].closed
    assert client.get("/api/sessions").json()["items"] == []


def test_streaming_resume_model_switch_and_secret_redaction(workspace):
    client, app, requests, clients, _ = workspace
    profile = add_model(client)
    sid = add_session(client, profile)
    assert client.delete(f"/api/models/{profile}").status_code == 409
    with client.websocket_connect(f"/api/sessions/{sid}/ws", headers=ORIGIN) as socket:
        assert socket.receive_json()["type"] == "ready"
        submit(socket, "第一轮研究")
        events = collect(socket)
        assert any(e["type"] == "delta" for e in events)
        assert not events[-1]["failed"]
        assert next(e for e in events if e["type"] == "usage")["usage"]["input_tokens"] == 10
        assert events[-1]["session"]["usage"]["cache_read_input_tokens"] is None
        submit(socket, "secret", request_id="r2")
        events = collect(socket)
        assert SECRET not in json.dumps(events)
        streamed = "".join(e["text"] for e in events if e["type"] == "delta")
        assert SECRET not in streamed
        assert "[已隐藏凭据]" in streamed
    assert len(requests[-1].messages) >= 3
    assert all(c.closed for c in clients)
    assert SECRET not in client.get(f"/api/sessions/{sid}").text
    # A fresh app reads the same file-backed history.
    with TestClient(create_app(app.state.workspace.cwd), base_url="http://localhost") as restarted:
        restored = restarted.get(f"/api/sessions/{sid}").json()
        assert restored["summary"] == "第一轮研究"
        assert len(restored["messages"]) == 4
        assert restored["usage"]["input_tokens"] == 20
        assert restored["usage"]["output_tokens"] == 10
        assert "runtime_context" not in json.dumps(restored["messages"])
    second = add_model(client, key="another-key", label="Second")
    assert client.patch(f"/api/sessions/{sid}", json={"profile_id": second}).status_code == 200
    assert client.delete(f"/api/models/{profile}").status_code == 200


def test_cancel_disconnect_and_global_busy(workspace):
    client, app, _, _, _ = workspace
    profile = add_model(client)
    first, second = add_session(client, profile), add_session(client, profile)
    with client.websocket_connect(f"/api/sessions/{first}/ws", headers=ORIGIN) as one:
        one.receive_json()
        submit(one, "slow")
        collect(one, "delta")
        with client.websocket_connect(f"/api/sessions/{second}/ws", headers=ORIGIN) as two:
            two.receive_json()
            submit(two, "another")
            events = collect(two)
            assert events[-1]["failed"]
            assert any("另一个会话" in e.get("message", "") for e in events)
        one.send_json({"type": "cancel", "request_id": "r1"})
        assert collect(one)[-1]["cancelled"]
        assert not app.state.workspace.lock.locked()
        submit(one, "slow", request_id="r2")
        collect(one, "delta")
    # Websocket teardown waits for cancellation and persistence.
    assert not app.state.workspace.lock.locked()
    record = client.get(f"/api/sessions/{first}").json()
    assert record["messages"][-1]["text"] == "部分回复"
    assert len(record["messages"]) == 4


@pytest.mark.parametrize("allow", [True, False])
def test_write_permission_response(workspace, allow):
    client, _, _, _, cwd = workspace
    sid = add_session(client, add_model(client))
    with client.websocket_connect(f"/api/sessions/{sid}/ws", headers=ORIGIN) as socket:
        socket.receive_json()
        submit(socket, "write")
        events = collect(socket, "prompt")
        prompt = events[-1]
        assert prompt["kind"] == "permission"
        assert prompt["tool_label"] == "写入文件"
        assert "允许此次操作" in prompt["message"]
        assert "/permissions" not in prompt["message"]
        socket.send_json(
            {
                "type": "response",
                "request_id": "r1",
                "prompt_id": prompt["prompt_id"],
                "answer": "allow" if allow else "deny",
            }
        )
        response = collect(socket, ("prompt", "done"))[-1]
        if response["type"] == "prompt":
            assert response["kind"] == "edit" and allow
            socket.send_json(
                {
                    "type": "response",
                    "request_id": "r1",
                    "prompt_id": response["prompt_id"],
                    "answer": "allow",
                }
            )
            response = collect(socket)[-1]
        assert not response["failed"]
    assert (cwd / "note.txt").exists() == allow


def test_edit_approval_reject_does_not_write(workspace):
    client, _, _, _, cwd = workspace
    (cwd / "note.txt").write_text("original")
    sid = add_session(client, add_model(client))
    with client.websocket_connect(f"/api/sessions/{sid}/ws", headers=ORIGIN) as socket:
        socket.receive_json()
        submit(socket, "edit")
        prompt = collect(socket, "prompt")[-1]
        if prompt["kind"] == "permission":
            socket.send_json(
                {
                    "type": "response",
                    "request_id": "r1",
                    "prompt_id": prompt["prompt_id"],
                    "answer": "allow",
                }
            )
            prompt = collect(socket, "prompt")[-1]
        assert prompt["kind"] == "edit"
        assert "original" in prompt["diff"]
        socket.send_json(
            {
                "type": "response",
                "request_id": "r1",
                "prompt_id": prompt["prompt_id"],
                "answer": "deny",
            }
        )
        collect(socket)
    assert (cwd / "note.txt").read_text() == "original"


def test_research_skill_enable_invoke_and_disable_next_turn(workspace):
    client, _, requests, _, _ = workspace
    cards = client.get("/api/skills").json()["items"]
    research = [item for item in cards if item["category"] != "技能管理"]
    assert len(research) == 2 and all(s["enabled"] for s in research)
    assert all("sample" not in card for card in cards)
    sample = "financial-statement-analysis"
    assert client.patch(f"/api/skills/{sample}", json={"enabled": True}).status_code == 200
    sid = add_session(client, add_model(client))
    with client.websocket_connect(f"/api/sessions/{sid}/ws", headers=ORIGIN) as socket:
        socket.receive_json()
        submit(socket, "/financial-statement-analysis 测试公司")
        events = collect(socket)
        assert not events[-1]["failed"]
        assert "financial-statement-analysis" in requests[-1].system_prompt
        assert (
            events[-1]["session"]["messages"][0]["text"] == "/financial-statement-analysis 测试公司"
        )
        client.patch(f"/api/skills/{sample}", json={"enabled": False})
        submit(socket, "下一轮", request_id="r2")
        collect(socket)
        assert "**financial-statement-analysis**" not in requests[-1].system_prompt
    card = next(
        item
        for item in client.get("/api/skills").json()["items"]
        if item["id"] == "analysis-modeling"
    )
    assert card["enabled"]
    assert not next(s for s in card["skills"] if s["name"] == sample)["enabled"]


def test_packaged_skill_plugin_disable_and_enable_next_turn(workspace):
    client, _, requests, _, _ = workspace
    card = next(
        item
        for item in client.get("/api/skills").json()["items"]
        if item["id"] == "analysis-modeling"
    )
    assert card["enabled"] and "sample" not in card
    assert len(card["skills"]) == 4
    assert all("content" not in skill for skill in card["skills"])
    sid = add_session(client, add_model(client))
    with client.websocket_connect(f"/api/sessions/{sid}/ws", headers=ORIGIN) as socket:
        socket.receive_json()
        submit(socket, "分析财报")
        collect(socket)
        assert "**financial-statement-analysis**" in requests[-1].system_prompt
        assert (
            client.patch("/api/skills/analysis-modeling", json={"enabled": False}).status_code
            == 200
        )
        submit(socket, "下一轮", request_id="r2")
        collect(socket)
        assert "**financial-statement-analysis**" not in requests[-1].system_prompt
        assert (
            client.patch("/api/skills/analysis-modeling", json={"enabled": True}).status_code == 200
        )
        submit(socket, "再下一轮", request_id="r3")
        collect(socket)
        assert "**financial-statement-analysis**" in requests[-1].system_prompt


def test_unconfigured_model_and_api_failures_are_recoverable(workspace):
    client, _, _, _, _ = workspace
    profile = add_model(client, key="")
    sid = add_session(client, profile)
    with client.websocket_connect(f"/api/sessions/{sid}/ws", headers=ORIGIN) as socket:
        socket.receive_json()
        submit(socket, "hello")
        events = collect(socket)
        assert events[-1]["failed"] and any("API Key" in e.get("message", "") for e in events)
        client.put(
            f"/api/models/{profile}",
            json={
                "label": "Test",
                "api_format": "openai",
                "model": "test-model",
                "context_window_tokens": 200_000,
                "api_key": SECRET,
            },
        )
        submit(socket, "fail", request_id="r2")
        events = collect(socket)
        assert events[-1]["failed"] and SECRET not in json.dumps(events)
        submit(socket, "重新开始", request_id="r3")
        assert not collect(socket)[-1]["failed"]


def test_question_response_and_stale_request_id(workspace):
    client, _, _, _, _ = workspace
    sid = add_session(client, add_model(client))
    with client.websocket_connect(f"/api/sessions/{sid}/ws", headers=ORIGIN) as socket:
        socket.receive_json()
        submit(socket, "ask")
        prompt = collect(socket, "prompt")[-1]
        assert prompt["kind"] == "question"
        socket.send_json(
            {
                "type": "response",
                "request_id": "stale",
                "prompt_id": prompt["prompt_id"],
                "answer": "错误回复",
            }
        )
        socket.send_json(
            {
                "type": "response",
                "request_id": "r1",
                "prompt_id": prompt["prompt_id"],
                "answer": "最近一年",
            }
        )
        events = collect(socket)
        record = client.app.state.workspace.store.load_by_id(client.app.state.workspace.cwd, sid)
        assert "最近一年" in json.dumps(record["messages"], ensure_ascii=False)
        operations = [
            e["message"]
            for e in events
            if e["type"] == "message" and e["message"]["role"] == "activity"
        ]
        assert operations and all("tool_input" not in row and not row["text"] for row in operations)
        assert "错误回复" not in json.dumps(events, ensure_ascii=False)


def test_websocket_origins_duplicate_windows_and_pending_approval_cancel(workspace):
    from fastapi import WebSocketDisconnect

    client, app, _, _, cwd = workspace
    sid = add_session(client, add_model(client))
    for headers in ({"host": "localhost"}, {"host": "localhost", "origin": "https://evil.example"}):
        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect(f"/api/sessions/{sid}/ws", headers=headers),
        ):
            pass
    with client.websocket_connect(f"/api/sessions/{sid}/ws", headers=ORIGIN) as socket:
        socket.receive_json()
        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect(f"/api/sessions/{sid}/ws", headers=ORIGIN),
        ):
            pass
        submit(socket, "write")
        collect(socket, "prompt")
        socket.send_json({"type": "cancel", "request_id": "r1"})
        assert collect(socket)[-1]["cancelled"]
        assert not app.state.workspace.connections[sid].prompts
    assert not (cwd / "note.txt").exists()


def test_probe_incomplete_response_is_not_success(workspace, monkeypatch):
    client, _, _, _, _ = workspace

    class IncompleteClient(FakeClient):
        async def stream_message(self, request):
            yield ApiTextDeltaEvent("incomplete")

    monkeypatch.setattr(
        "openharness.web.app._resolve_api_client_from_settings", lambda _: IncompleteClient([])
    )
    response = client.post(f"/api/models/{add_model(client)}/test")
    assert not response.json()["ok"]


def test_cli_auth_exit_does_not_terminate_web_server(workspace, monkeypatch):
    client, app, _, _, _ = workspace
    sid = add_session(client, add_model(client))

    def unavailable(settings):
        raise SystemExit(1)

    monkeypatch.setattr("openharness.runtime._resolve_api_client_from_settings", unavailable)
    with client.websocket_connect(f"/api/sessions/{sid}/ws", headers=ORIGIN) as socket:
        socket.receive_json()
        submit(socket, "hello")
        assert collect(socket)[-1]["failed"]
    assert not app.state.workspace.lock.locked()
    assert client.get("/api/health").status_code == 200


def test_skill_switch_does_not_change_an_in_flight_turn(workspace):
    client, _, requests, _, _ = workspace
    sample = "financial-statement-analysis"
    client.patch(f"/api/skills/{sample}", json={"enabled": True})
    sid = add_session(client, add_model(client))
    with client.websocket_connect(f"/api/sessions/{sid}/ws", headers=ORIGIN) as socket:
        socket.receive_json()
        submit(socket, "skill-transition")
        prompt = collect(socket, "prompt")[-1]
        client.patch(f"/api/skills/{sample}", json={"enabled": False})
        socket.send_json(
            {
                "type": "response",
                "request_id": "r1",
                "prompt_id": prompt["prompt_id"],
                "answer": "invoke-skill",
            }
        )
        events = collect(socket)
        record = client.app.state.workspace.store.load_by_id(client.app.state.workspace.cwd, sid)
        output = json.dumps(record["messages"], ensure_ascii=False)
        assert "简化ROE不是加权平均ROE" in output
        operations = [
            e["message"]
            for e in events
            if e["type"] == "message" and e["message"]["role"] == "activity"
        ]
        assert operations and all("tool_input" not in row and not row["text"] for row in operations)
        submit(socket, "下一轮", request_id="r2")
        collect(socket)
        assert "**financial-statement-analysis**" not in requests[-1].system_prompt


@pytest.mark.parametrize("profile_id", ["codex", "claude-subscription", "copilot"])
def test_subscription_profiles_selectable_without_api_key_conversion(
    workspace, monkeypatch, profile_id
):
    from openharness.config.settings import ResolvedAuth
    from openharness.web.catalog import profile_settings
    from types import SimpleNamespace

    client, _, _, _, _ = workspace
    monkeypatch.setattr(
        Settings,
        "resolve_auth",
        lambda self: ResolvedAuth(
            provider=self.provider,
            auth_kind="oauth",
            value="subscription-test-token",
            source="external:test",
        ),
    )
    monkeypatch.setattr("openharness.api.copilot_auth.load_copilot_auth", lambda: SimpleNamespace())
    profiles = client.get("/api/models").json()["items"]
    profile = next(item for item in profiles if item["id"] == profile_id)
    assert profile["supported"] and profile["configured"] and not profile["editable"]
    assert "subscription-test-token" not in json.dumps(profiles)
    assert client.post(f"/api/models/{profile_id}/activate").status_code == 200
    session = add_session(client, profile_id)
    assert client.get(f"/api/sessions/{session}").json()["profile_id"] == profile_id
    before = profile_settings(profile_id).resolve_profile()[1].auth_source
    assert (
        client.put(
            f"/api/models/{profile_id}",
            json={
                "label": "incorrect API conversion",
                "api_format": "openai",
                "model": "test",
            },
        ).status_code
        == 400
    )
    assert profile_settings(profile_id).resolve_profile()[1].auth_source == before


def test_attachment_upload_submit_isolation_restart_and_cleanup(workspace):
    from openharness.research.store import ResearchStore
    from openharness.utils.session_files import SessionFiles

    client, app, requests, _, cwd = workspace
    sid = add_session(client, add_model(client))
    second = add_session(client, add_model(client, label="Other"))
    response = client.post(
        f"/api/sessions/{sid}/attachments",
        files=[("files", ("report.txt", b"Consolidated revenue 100", "text/plain"))],
    )
    assert response.status_code == 201, response.text
    attachment = response.json()["items"][0]
    assert attachment["status"] == "ready"
    assert client.get(f"/api/sessions/{sid}/attachments").json()["items"] == [attachment]
    assert client.get(f"/api/sessions/{second}/attachments").json()["items"] == []
    assert (
        client.delete(f"/api/sessions/{second}/attachments/{attachment['id']}").status_code == 404
    )
    with client.websocket_connect(f"/api/sessions/{sid}/ws", headers=ORIGIN) as socket:
        socket.receive_json()
        socket.send_json(
            {
                "type": "submit",
                "request_id": "r1",
                "text": "Analyze",
                "attachment_ids": [attachment["id"]],
            }
        )
        events = collect(socket)
        assert not events[-1]["failed"]
        user = next(m for m in reversed(requests[-1].messages) if m.role == "user")
        assert "text.md" in user.text and "parsed.json" in user.text and "外部资料" in user.text
    with client.websocket_connect(f"/api/sessions/{second}/ws", headers=ORIGIN) as socket:
        socket.receive_json()
        socket.send_json(
            {
                "type": "submit",
                "request_id": "r1",
                "text": "Analyze",
                "attachment_ids": [attachment["id"]],
            }
        )
        events = collect(socket)
        assert events[-1]["failed"] and any("不属于" in e.get("message", "") for e in events)
    directory = ResearchStore(cwd, sid).directory
    output = cwd / "report.md"
    output.write_text("Report")
    artifact = SessionFiles(directory).register(
        output, task_id="task1", status="partial", kind="financial"
    )
    endpoint = f"/api/sessions/{sid}/artifacts/{artifact['id']}/download"
    assert client.get(endpoint).content == b"Report"
    assert (
        client.get(f"/api/sessions/{second}/artifacts/{artifact['id']}/download").status_code == 404
    )
    restarted = create_app(str(cwd))
    with TestClient(restarted, base_url="http://localhost") as reclient:
        assert reclient.get(endpoint).content == b"Report"
        assert reclient.get(f"/api/sessions/{sid}/attachments").json()["items"] == [attachment]
    assert client.delete(f"/api/sessions/{sid}").status_code == 200
    assert not directory.exists() and client.get(endpoint).status_code == 404


def test_attachment_failures_limits_and_server_id_validation(workspace, monkeypatch):
    client, _, _, _, _ = workspace
    sid = add_session(client, add_model(client))
    endpoint = f"/api/sessions/{sid}/attachments"
    assert client.post(endpoint, files={"files": ("a.exe", b"binary")}).status_code == 422
    response = client.post(endpoint, files={"files": ("bad.pdf", b"not-pdf")})
    assert response.status_code == 201 and response.json()["items"][0]["status"] == "failed"
    assert (
        client.post(
            endpoint, files=[("files", (f"{i}.txt", b"data")) for i in range(11)]
        ).status_code
        == 400
    )
    monkeypatch.setattr("openharness.web.app.MAX_DOCUMENT_BYTES", 10)
    assert client.post(endpoint, files={"files": ("large.txt", b"x" * 11)}).status_code == 413
    assert client.get(f"/api/sessions/{sid}/artifacts/arbitrary-path/download").status_code == 404


def test_model_context_budget_round_trip_and_omitted_edit_preserves_it(workspace):
    client, _, _, _, _ = workspace
    profile = add_model(client)
    row = next(item for item in client.get("/api/models").json()["items"] if item["id"] == profile)
    assert row["context_window_tokens"] == 200_000
    payload = {
        "label": "Budgeted model",
        "api_format": "openai",
        "model": "test-model",
        "auto_compact_threshold_tokens": 12345,
    }
    assert client.put(f"/api/models/{profile}", json=payload).status_code == 200
    from openharness.web.catalog import profile_settings

    settings = profile_settings(profile)
    assert settings.context_window_tokens == 200_000
    assert settings.auto_compact_threshold_tokens == 12345
    assert settings.resolve_auth().value == SECRET
    payload.update(context_window_tokens=None, auto_compact_threshold_tokens=None)
    assert client.put(f"/api/models/{profile}", json=payload).status_code == 200
    assert profile_settings(profile).context_window_tokens is None
    tested = client.post(f"/api/models/{profile}/test").json()
    assert not tested["ok"] and "context_window_tokens" in tested["message"]


@pytest.mark.parametrize("field", ["context_window_tokens", "auto_compact_threshold_tokens"])
def test_model_rejects_nonpositive_budget_fields(workspace, field):
    client, _, _, _, _ = workspace
    response = client.post(
        "/api/models",
        json={"label": "Invalid budget", "api_format": "openai", "model": "test-model", field: 0},
    )
    assert response.status_code == 422


def test_skill_catalog_is_metadata_only_and_details_are_selected(workspace, monkeypatch):
    from pathlib import Path
    from openharness.plugins.loader import BUNDLED_PLUGINS_DIR

    client, _, _, _, _ = workspace
    original = Path.read_text
    reads = []

    def read(path, *args, **kwargs):
        if path.name == "SKILL.md" and path.is_relative_to(BUNDLED_PLUGINS_DIR):
            reads.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    cards = client.get("/api/skills").json()["items"]
    assert len(cards) == 2 and not reads
    assert all("content" not in skill for card in cards for skill in card["skills"])
    selected = client.get("/api/skills/report-generation/industry-commentary")
    assert selected.status_code == 200 and "行业点评" in selected.json()["content"]
    assert len(reads) == 1 and reads[0].parent.name == "industry-commentary"
    assert (
        client.patch("/api/skills/industry-commentary", json={"enabled": False}).status_code == 200
    )
    assert client.get("/api/skills/report-generation/industry-commentary").status_code == 409
    assert len(reads) == 1
    assert (
        client.patch("/api/skills/industry-commentary", json={"enabled": True}).status_code == 200
    )
    assert client.patch("/api/skills/report-generation", json={"enabled": False}).status_code == 200
    assert client.get("/api/skills/report-generation/industry-commentary").status_code == 409


def test_legacy_skill_configuration_and_explicit_switches(workspace):
    client, _, _, _, _ = workspace
    from openharness.config.settings import load_settings

    settings = load_settings().model_copy(
        update={"enabled_plugins": {"financial-statement-analysis": False}}
    )
    save_settings(settings)
    cards = client.get("/api/skills").json()["items"]
    package = next(card for card in cards if card["id"] == "analysis-modeling")
    assert package["enabled"]
    assert not next(
        skill for skill in package["skills"] if skill["name"] == "financial-statement-analysis"
    )["enabled"]
    assert (
        client.patch("/api/skills/financial-statement-analysis", json={"enabled": True}).status_code
        == 200
    )
    assert (
        client.get("/api/skills/analysis-modeling/financial-statement-analysis").status_code == 200
    )
