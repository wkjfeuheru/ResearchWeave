import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

from openharness.api.client import (
    ApiMessageRequest,
    ApiTextDeltaEvent,
    ApiMessageCompleteEvent,
    ApiRetryEvent,
)
from openharness.api.retry import stream_with_retry
from openharness.api.usage import UsageSnapshot
from openharness.engine.messages import ConversationMessage
from openharness.hooks import HookExecutor, HookExecutionContext, HookEvent
from openharness.hooks.loader import HookRegistry
from openharness.hooks.schemas import (
    HttpHookDefinition,
    CommandHookDefinition,
    PromptHookDefinition,
)
from openharness.hooks.safety import post_hook
from openharness.services.operations import OperationStore


@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr("openharness.api.retry.retry_delay", lambda *args: 0)


@pytest.mark.asyncio
async def test_failed_stream_discarded_and_every_attempt_persisted(tmp_path):
    calls = 0
    records = []
    request = ApiMessageRequest(
        model="test", messages=[], max_tokens=100, attempt_callback=records.append
    )

    async def stream(request):
        nonlocal calls
        calls += 1
        yield ApiTextDeltaEvent(text="discard me" if calls == 1 else "correct")
        if calls == 1:
            raise ConnectionError("broken stream")
        yield ApiMessageCompleteEvent(
            ConversationMessage.from_user_text("correct"), UsageSnapshot(usage_reported=False)
        )

    events = [e async for e in stream_with_retry(stream, request, translate=lambda e: e)]
    assert "".join(e.text for e in events if isinstance(e, ApiTextDeltaEvent)) == "correct"
    assert len([e for e in events if isinstance(e, ApiRetryEvent)]) == 1
    assert len({r["attempt_id"] for r in records}) == 2
    assert all(r["usage_status"] == "unknown" and r["usage"] is None for r in records)
    with OperationStore(tmp_path / "data/executions/operations.sqlite3").connect() as db:
        assert db.execute("SELECT count(*) FROM api_attempts").fetchone()[0] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,text",
    [
        (401, "authentication"),
        (403, "forbidden"),
        (400, "bad parameter"),
        (429, "insufficient_quota"),
        (400, "prompt too long"),
    ],
)
async def test_non_retryable_provider_errors(status, text):
    calls = 0

    async def stream(request):
        nonlocal calls
        calls += 1
        if False:
            yield None
        response = httpx.Response(status, request=httpx.Request("POST", "https://example.com"))
        raise httpx.HTTPStatusError(text, request=response.request, response=response)

    with pytest.raises(httpx.HTTPStatusError):
        async for _ in stream_with_retry(
            stream, ApiMessageRequest(model="test", messages=[]), translate=lambda e: e
        ):
            pass
    assert calls == 1


@pytest.mark.asyncio
async def test_api_cancel_never_retries():
    calls = 0

    async def stream(request):
        nonlocal calls
        calls += 1
        if False:
            yield None
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        async for _ in stream_with_retry(
            stream, ApiMessageRequest(model="test", messages=[]), translate=lambda e: e
        ):
            pass
    assert calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url", ["http://127.0.0.1/a", "http://localhost/a", "http://10.0.0.1/a", "http://[::1]/a"]
)
async def test_hook_private_target_blocked_before_http(url, monkeypatch):
    client = AsyncMock()
    monkeypatch.setattr("openharness.hooks.safety.httpx.AsyncClient", client)
    with pytest.raises(ValueError):
        await post_hook(HttpHookDefinition(url=url), "pre_tool_use", {"secret": "value"})
    client.assert_not_called()


@pytest.mark.asyncio
async def test_trusted_http_redirect_not_followed_and_payload_redacted(monkeypatch):
    calls = []

    def handle(request):
        calls.append(request)
        assert b"private-key" not in request.content
        return httpx.Response(307, headers={"location": "http://127.0.0.1/stolen"})

    original = httpx.AsyncClient
    monkeypatch.setattr(
        "openharness.hooks.safety.httpx.AsyncClient",
        lambda **kw: original(transport=httpx.MockTransport(handle), **kw),
    )
    with pytest.raises(ValueError, match="redirect"):
        await post_hook(
            HttpHookDefinition(
                url="http://internal.example/hook", trusted_origins=["http://internal.example"]
            ),
            "event",
            {"api_key": "private-key"},
        )
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_command_hook_environment_and_output_bound(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_API_TOKEN", "never-inherit")
    registry = HookRegistry()
    registry.register(
        HookEvent.PRE_TOOL_USE, CommandHookDefinition(command='printf "%s" "${MY_API_TOKEN-unset}"')
    )
    executor = HookExecutor(registry, HookExecutionContext(tmp_path, AsyncMock(), "test"))
    result = await executor.execute(HookEvent.PRE_TOOL_USE, {})
    assert result.results[0].output == "unset"
    registry.register(
        HookEvent.PRE_TOOL_USE,
        CommandHookDefinition(command="yes data", max_output_bytes=256, block_on_failure=True),
    )
    result = await asyncio.wait_for(executor.execute(HookEvent.PRE_TOOL_USE, {}), 3)
    assert result.blocked


@pytest.mark.asyncio
@pytest.mark.parametrize("blocking", [True, False])
async def test_invalid_model_hook_json_obeys_failure_policy(tmp_path, blocking):
    class Client:
        async def stream_message(self, request):
            yield ApiMessageCompleteEvent(
                ConversationMessage.from_user_text("yes"), UsageSnapshot()
            )

    registry = HookRegistry()
    registry.register(
        HookEvent.PRE_TOOL_USE, PromptHookDefinition(prompt="check", block_on_failure=blocking)
    )
    result = await HookExecutor(registry, HookExecutionContext(tmp_path, Client(), "test")).execute(
        HookEvent.PRE_TOOL_USE, {}
    )
    assert not result.results[0].success
    assert result.blocked is blocking


@pytest.mark.asyncio
async def test_hook_cancel_kills_child_process_group(tmp_path):
    import os
    import shlex
    import sys
    from pathlib import Path

    # The child cannot produce its side effect before the test releases it.
    # A fixed sleep raced the event loop under concurrent Python/browser suites.
    child = (
        "import os,time; from pathlib import Path; "
        "Path('child.pid').write_text(str(os.getpid())); Path('ready').touch(); "
        "exec(\"while not Path('release').exists():\\n time.sleep(0.005)\"); "
        "Path('leaked').touch()"
    )
    registry = HookRegistry()
    registry.register(
        HookEvent.PRE_TOOL_USE,
        CommandHookDefinition(
            command=f"{shlex.quote(sys.executable)} -c {shlex.quote(child)} & wait"
        ),
    )
    executor = HookExecutor(registry, HookExecutionContext(tmp_path, AsyncMock(), "test"))
    pending = asyncio.create_task(executor.execute(HookEvent.PRE_TOOL_USE, {}))

    async def ready():
        while not (tmp_path / "ready").exists():
            await asyncio.sleep(0.005)

    try:
        await asyncio.wait_for(ready(), 2)
        pid = int((tmp_path / "child.pid").read_text())
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        (tmp_path / "release").touch()

        async def stopped():
            while True:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    return
                proc = Path(f"/proc/{pid}/stat")
                try:
                    if proc.is_file() and proc.read_text().split()[2] == "Z":
                        return  # Exited process awaiting reaping cannot perform effects.
                except FileNotFoundError:
                    return
                await asyncio.sleep(0.005)

        await asyncio.wait_for(stopped(), 2)
        assert not (tmp_path / "leaked").exists()
    finally:
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_hook_required_sandbox_cannot_fallback_to_host(tmp_path, monkeypatch):
    from openharness.config import Settings
    from openharness.config.settings import SandboxSettings

    monkeypatch.setattr("openharness.sandbox.adapter.shutil.which", lambda name: None)
    registry = HookRegistry()
    registry.register(
        HookEvent.PRE_TOOL_USE,
        CommandHookDefinition(command="touch escaped", block_on_failure=True),
    )
    executor = HookExecutor(
        registry,
        HookExecutionContext(
            tmp_path,
            AsyncMock(),
            "test",
            settings=Settings(sandbox=SandboxSettings(enabled=True, fail_if_unavailable=True)),
        ),
    )
    result = await executor.execute(HookEvent.PRE_TOOL_USE, {})
    assert result.blocked and not (tmp_path / "escaped").exists()


@pytest.mark.asyncio
async def test_provider_retry_reaches_engine_without_partial_text(tmp_path, monkeypatch):
    from openharness.api.openai_client import OpenAICompatibleClient
    from openharness.config.settings import PermissionSettings
    from openharness.engine.query_engine import QueryEngine
    from openharness.permissions.checker import PermissionChecker
    from openharness.tools.base import ToolRegistry
    from openharness.engine.stream_events import AssistantTextDelta
    from openharness.engine.messages import TextBlock

    provider = OpenAICompatibleClient("test-key")
    calls = 0

    async def stream_once(request):
        nonlocal calls
        calls += 1
        yield ApiTextDeltaEvent("obsolete partial" if calls == 1 else "complete answer")
        if calls == 1:
            raise ConnectionError("reset")
        yield ApiMessageCompleteEvent(
            ConversationMessage(role="assistant", content=[TextBlock(text="complete answer")]),
            UsageSnapshot(),
        )

    monkeypatch.setattr(provider, "_stream_once", stream_once)
    engine = QueryEngine(
        api_client=provider,
        tool_registry=ToolRegistry(),
        permission_checker=PermissionChecker(PermissionSettings()),
        cwd=tmp_path,
        model="gpt-4o",
        system_prompt="test",
        context_window_tokens=128000,
    )
    try:
        events = [event async for event in engine.submit_message("answer")]
        assert (
            "".join(event.text for event in events if isinstance(event, AssistantTextDelta))
            == "complete answer"
        )
        assert engine.messages[-1].text == "complete answer"
        assert calls == 2
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_http_hook_pins_validated_ip_and_retains_host_and_tls_name(monkeypatch):
    import ipaddress

    async def resolve(host, port):
        assert host == "hooks.example.com"
        return {ipaddress.ip_address("93.184.216.34")}

    monkeypatch.setattr("openharness.utils.network_guard._resolve_host_addresses", resolve)

    def handle(request):
        assert request.url.host == "93.184.216.34"
        assert request.headers["host"] == "hooks.example.com"
        assert request.extensions["sni_hostname"] == "hooks.example.com"
        return httpx.Response(200, content=b"ok")

    original = httpx.AsyncClient
    monkeypatch.setattr(
        "openharness.hooks.safety.httpx.AsyncClient",
        lambda **kw: original(transport=httpx.MockTransport(handle), **kw),
    )
    assert await post_hook(
        HttpHookDefinition(url="https://hooks.example.com/ingest"), "event", {}
    ) == (200, "ok")


def test_json_response_secrets_are_redacted():
    from openharness.hooks.safety import redact

    value = redact(
        '{"nested":{"api_key":"private-response","authorization":"Basic private"},"ok":true}'
    )
    assert "private-response" not in value and "Basic private" not in value
    assert "true" in value and "[REDACTED]" in value
