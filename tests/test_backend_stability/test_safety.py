"""Security regressions use real files, receipts, admitted tools and anchored roots."""

import asyncio
import os
import stat
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from openharness.config import Settings
from openharness.config.settings import save_settings, load_settings
from openharness.engine.messages import ConversationMessage
from openharness.api.usage import UsageSnapshot
from openharness.permissions.modes import PermissionMode
from openharness.services.operations import OperationStore
from openharness.services.tool_execution import ToolExecutionService
from openharness.services.session_storage import save_session_snapshot
from openharness.skills.loader import load_skills_from_dirs
from openharness.plugins.loader import discover_plugin_paths, load_plugin
from openharness.tools.base import ToolExecutionContext
from openharness.tools.bash_tool import BashTool, BashToolInput
from openharness.utils.fs import atomic_write_text
from openharness.utils.session_files import SessionFiles
from tests.test_harness.test_execution import setup, Write, ledger


def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


@pytest.mark.skipif(
    os.name != "posix",
    reason="POSIX permission-bit assertions; other platforms document ACL limits",
)
def test_private_storage_new_and_existing_modes_do_not_chmod_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "private-data"))
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "private-config"))
    os.chmod(tmp_path, 0o755)
    normal = tmp_path / "user.txt"
    normal.write_text("ordinary workspace")
    os.chmod(normal, 0o644)
    path = save_session_snapshot(
        cwd=tmp_path,
        model="test",
        system_prompt="private",
        messages=[ConversationMessage.from_user_text("private conversation")],
        usage=UsageSnapshot(),
        session_id="a" * 12,
    )
    assert mode(path) == mode(path.parent / ("session-" + "a" * 12 + ".json")) == 0o600
    assert mode(path.parent) == mode(tmp_path / "private-data") == 0o700
    os.chmod(path, 0o666)
    save_session_snapshot(
        cwd=tmp_path,
        model="test",
        system_prompt="secret",
        messages=[],
        usage=UsageSnapshot(),
        session_id="a" * 12,
    )
    assert mode(path) == 0o600
    save_settings(Settings())
    config = tmp_path / "private-config/settings.json"
    os.chmod(config, 0o666)
    load_settings()
    assert mode(config) == 0o600
    store = OperationStore(tmp_path / "database/operations.sqlite3")
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "INSERT INTO api_attempts(attempt_id, request_id, record) VALUES ('audit','req','{}')"
        )
        for candidate in (
            store.path,
            Path(str(store.path) + "-wal"),
            Path(str(store.path) + "-shm"),
        ):
            assert candidate.exists() and mode(candidate) == 0o600
        db.rollback()
    assert mode(store.path.parent) == 0o700
    files = SessionFiles(tmp_path / "session")
    uploaded = files.upload("sensitive.txt", b"private attachment")
    _, directory = files.attachment(uploaded["id"])
    assert mode(directory) == mode(files.root) == 0o700
    assert all(mode(p) == 0o600 for p in directory.iterdir() if p.is_file())
    files.register(normal, task_id=None, status="ready", kind="note")
    assert mode(tmp_path) == 0o755 and mode(normal) == 0o644


@pytest.mark.skipif(os.name != "posix", reason="POSIX chmod failure semantics")
def test_explicit_sensitive_mode_failure_does_not_replace_existing_file(tmp_path, monkeypatch):
    target = tmp_path / "secret"
    target.write_text("old")
    monkeypatch.setattr(
        "openharness.utils.fs.os.chmod",
        lambda *args: (_ for _ in ()).throw(PermissionError("chmod denied")),
    )
    with pytest.raises(PermissionError):
        atomic_write_text(target, "new", mode=0o600)
    assert target.read_text() == "old" and not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("same_call", [True, False])
async def test_claim_wait_is_bounded_and_does_not_modify_live_owner(
    tmp_path, monkeypatch, same_call
):
    tool, context = setup(tmp_path, monkeypatch)
    tool.wait = True
    tool.contract = {**tool.contract, "timeout_seconds": 10.0}
    active = asyncio.create_task(
        ToolExecutionService().execute(context, tool.name, "active", {"value": 1})
    )
    await asyncio.wait_for(tool.entered.wait(), 2)
    waiting = ToolExecutionService(claim_wait_seconds=0.025)
    result = await waiting.execute(
        context, tool.name, "active" if same_call else "different", {"value": 1}
    )
    assert result.is_error and result.result_metadata["error_code"] == (
        "operation_in_progress" if same_call else "resource_conflict"
    )
    with ledger(tmp_path).connect() as db:
        row = db.execute("SELECT status,owner FROM operations WHERE call_id='active'").fetchone()
        assert row["status"] == "running" and row["owner"] != waiting.owner
    assert tool.calls == 1
    active.cancel()
    with pytest.raises(asyncio.CancelledError):
        await active


async def test_cancel_waiter_keeps_original_owner_and_never_runs_hooks(tmp_path, monkeypatch):
    tool, context = setup(tmp_path, monkeypatch)
    tool.wait = True
    tool.contract = {**tool.contract, "timeout_seconds": 10.0}
    active = asyncio.create_task(
        ToolExecutionService().execute(context, tool.name, "call", {"value": 9})
    )
    await tool.entered.wait()
    waiter = asyncio.create_task(
        ToolExecutionService().execute(context, tool.name, "call", {"value": 9})
    )
    await asyncio.sleep(0.01)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    with ledger(tmp_path).connect() as db:
        assert db.execute("SELECT status,attempts FROM operations").fetchone()[:] == ("running", 1)
    active.cancel()
    with pytest.raises(asyncio.CancelledError):
        await active
    assert tool.calls == 1


@pytest.mark.parametrize("permission", [PermissionMode.DEFAULT, PermissionMode.PLAN])
async def test_general_local_write_cannot_claim_readonly_exception(
    tmp_path, monkeypatch, permission
):
    class PretendRead(Write):
        contract = {"name": Write.name, "effect": "local_write"}

        def is_read_only(self, arguments):
            return True

    tool, context = setup(tmp_path, monkeypatch, PretendRead(), mode=permission)
    context.tool_metadata = {"research.write": True}
    result = await ToolExecutionService().execute(context, tool.name, "pretend", {"value": 1})
    assert result.is_error and tool.calls == 0 and not (tmp_path / "side_effect").exists()


@pytest.mark.parametrize("kind", ["directory", "entry_file"])
def test_skill_escape_rejected_before_frontmatter_is_read(tmp_path, monkeypatch, kind):
    approved, outside = tmp_path / "approved", tmp_path / "outside"
    approved.mkdir()
    outside.mkdir()
    (outside / "SKILL.md").write_text("# PRIVATE_EXTERNAL_CONTENT")
    if kind == "directory":
        (approved / "external").symlink_to(outside, target_is_directory=True)
    else:
        (approved / "local").mkdir()
        (approved / "local/SKILL.md").symlink_to(outside / "SKILL.md")
    read = AsyncMock()  # No read-discovery call should happen at all.
    monkeypatch.setattr("openharness.skills.loader.read_discovery_header", read)
    assert load_skills_from_dirs([approved]) == [] and read.call_count == 0


def test_selected_entry_rechecks_original_root_after_directory_replacement(tmp_path):
    approved = tmp_path / "approved"
    entry = approved / "local"
    entry.mkdir(parents=True)
    (entry / "SKILL.md").write_text("# Local\nApproved content")
    skill = load_skills_from_dirs([approved])[0]
    entry.rename(approved / "old")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_text("# External secret")
    entry.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="approved root"):
        skill.load_content()


def test_plugin_discovery_and_manifest_cannot_escape_approved_root(tmp_path, monkeypatch):
    approved, outside = tmp_path / "plugins", tmp_path / "outside"
    approved.mkdir()
    outside.mkdir()
    (outside / "plugin.json").write_text('{"name":"external"}')
    (approved / "external").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr("openharness.plugins.loader.BUNDLED_PLUGINS_DIR", approved)
    assert outside not in discover_plugin_paths(tmp_path)
    assert not any(p.name == "external" for p in discover_plugin_paths(tmp_path))
    local = approved / "local"
    local.mkdir()
    (local / "plugin.json").symlink_to(outside / "plugin.json")
    assert load_plugin(local, {}) is None
    (local / "plugin.json").unlink()
    (local / "plugin.json").write_text('{"name":"local","skills_dir":"../../outside"}')
    assert load_plugin(local, {}) is None


async def test_default_agent_shell_fails_closed_even_if_metadata_requests_host(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("openharness.sandbox.adapter.shutil.which", lambda name: None)
    settings = Settings()
    settings.sandbox.allow_trusted_host = True
    context = ToolExecutionContext(
        cwd=tmp_path, settings=settings, metadata={"allow_trusted_host": True}
    )
    result = await BashTool().execute(BashToolInput(command="touch escaped"), context)
    assert result.is_error and not (tmp_path / "escaped").exists()
    assert result.metadata["safety_level"] == "sandbox_required"


def test_automatic_user_skill_root_alias_does_not_scan_external_content(tmp_path, monkeypatch):
    from openharness.skills.loader import load_user_skills

    home, outside = tmp_path / "home", tmp_path / "outside"
    (home / ".agents").mkdir(parents=True)
    outside.mkdir()
    (outside / "SKILL.md").write_text("# UnapprovedExternal")
    (home / ".agents/skills").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(home / ".openharness"))
    assert all(skill.name != "UnapprovedExternal" for skill in load_user_skills())


async def test_explicit_main_host_mode_is_visible_before_execution(tmp_path, monkeypatch):
    from openharness.api.client import ApiMessageCompleteEvent
    from openharness.engine.query_engine import QueryEngine
    from openharness.engine.messages import ToolUseBlock, TextBlock
    from openharness.engine.stream_events import StatusEvent, ToolExecutionStarted
    from openharness.permissions.checker import PermissionChecker
    from openharness.config.settings import PermissionSettings
    from openharness.tools.base import ToolRegistry

    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "private-data"))

    class Client:
        calls = 0

        async def stream_message(self, request):
            self.calls += 1
            blocks = (
                [
                    ToolUseBlock(
                        id="host",
                        name="bash",
                        input={"command": "printf trusted > explicit-host.txt"},
                    )
                ]
                if self.calls == 1
                else [TextBlock(text="done")]
            )
            yield ApiMessageCompleteEvent(
                ConversationMessage(role="assistant", content=blocks), UsageSnapshot()
            )

    settings = Settings()
    settings.sandbox.allow_trusted_host = True
    settings.research_memory.enabled = False
    registry = ToolRegistry()
    registry.register(BashTool())
    engine = QueryEngine(
        api_client=Client(),
        tool_registry=registry,
        permission_checker=PermissionChecker(PermissionSettings(mode=PermissionMode.FULL_AUTO)),
        cwd=tmp_path,
        model="test",
        settings=settings,
        context_window_tokens=200000,
        system_prompt="test",
    )
    events = [event async for event in engine.submit_message("explicit trusted command")]
    assert any(isinstance(e, StatusEvent) and "宿主" in e.message for e in events), repr(events)
    notice = next(
        i for i, e in enumerate(events) if isinstance(e, StatusEvent) and "宿主" in e.message
    )
    started = next(i for i, e in enumerate(events) if isinstance(e, ToolExecutionStarted))
    assert notice < started and (tmp_path / "explicit-host.txt").read_text() == "trusted"


async def test_child_does_not_inherit_host_authority(tmp_path):
    from openharness.engine.query import QueryContext
    from openharness.engine.subagents import execute_subagent
    from openharness.permissions.capabilities import CapabilityContext
    from openharness.permissions.checker import PermissionChecker
    from openharness.config.settings import PermissionSettings
    from openharness.tools.base import ToolRegistry

    captured = []

    async def inspect(child, messages):
        captured.append(child.capabilities.allow_trusted_host)
        if False:
            yield None

    from unittest.mock import patch

    parent = QueryContext(
        api_client=AsyncMock(),
        tool_registry=ToolRegistry(),
        permission_checker=PermissionChecker(PermissionSettings()),
        cwd=tmp_path,
        model="test",
        system_prompt="test",
        max_tokens=100,
        capabilities=CapabilityContext(allow_trusted_host=True),
    )
    with patch("openharness.engine.query.run_query", inspect):
        await execute_subagent(
            parent,
            registry=ToolRegistry(),
            cwd=tmp_path,
            prompt="child",
            messages=[],
            metadata={},
            max_calls=1,
            timeout=1,
            transcript_path=tmp_path / "private/messages.json",
        )
    assert captured == [False]


def test_html_search_import_does_not_import_tavily_transport():
    import subprocess
    import sys

    script = """
import sys, importlib.abc
class BlockTavily(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname == 'openharness.utils.tavily_search':
            raise AssertionError('HTML must import independently of the Tavily transport')
sys.meta_path.insert(0, BlockTavily())
import openharness.tools.web_search_tool
assert 'openharness.utils.tavily_search' not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=20
    )
    assert result.returncode == 0, result.stderr


def test_private_file_handles_removed_sqlite_sidecar_without_recreating_it(tmp_path, monkeypatch):
    from openharness.utils.fs import private_file

    sidecar = tmp_path / "operations.sqlite3-wal"
    sidecar.write_text("ephemeral")
    original = os.open

    def removed(path, *args, **kwargs):
        if Path(path) == sidecar:
            sidecar.unlink()
        return original(path, *args, **kwargs)

    monkeypatch.setattr("openharness.utils.fs.os.open", removed)
    private_file(sidecar)
    assert not sidecar.exists()


async def test_cancel_propagates_even_if_tool_receipt_database_is_busy(tmp_path, monkeypatch):
    import sqlite3

    tool, context = setup(tmp_path, monkeypatch)
    tool.wait = True
    tool.contract = {**tool.contract, "timeout_seconds": 10.0}
    task = asyncio.create_task(
        ToolExecutionService().execute(context, tool.name, "cancel-audit", {"value": 1})
    )
    await tool.entered.wait()
    monkeypatch.setattr(
        OperationStore,
        "settle",
        lambda *a, **kw: (_ for _ in ()).throw(sqlite3.OperationalError("locked")),
    )
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with ledger(tmp_path).connect() as db:
        assert db.execute("SELECT status,attempts FROM operations").fetchone()[:] == ("running", 1)
    assert tool.calls == 1


@pytest.mark.parametrize("case", ["normal", "private", "stale"])
def test_legacy_export_import_is_host_scoped_and_rejects_private_or_stale_data(
    tmp_path, monkeypatch, case
):
    import json
    from openharness.tools.bash_tool import _register_legacy_exports
    from tests.test_research.test_conflicts import make_research, apply

    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))
    store, evs, _, _ = make_research(tmp_path)
    memory = store.load()
    baseline = (
        memory.current_context_id,
        memory.research_state.current_plan_id,
        memory.research_state.current_task_id,
    )
    output = tmp_path / "reports/report.md"
    output.parent.mkdir()
    output.write_text("declared output")
    path = store.path if case == "private" else output
    if case == "stale":
        apply(store, "create_plan", title="new scope", tasks=["new task"], reused_evidence_ids=evs)
    packet = bytearray(json.dumps({"files": [str(path)], "status": "candidate"}).encode())
    context = ToolExecutionContext(cwd=tmp_path)
    if case == "normal":
        files = _register_legacy_exports(packet, context, store, baseline)
        assert len(files) == 1 and files[0]["task_id"] == baseline[2]
        _, saved = SessionFiles(store.directory).artifact(files[0]["id"])
        assert saved.read_text() == "declared output"
    else:
        with pytest.raises(ValueError):
            _register_legacy_exports(packet, context, store, baseline)
        assert SessionFiles(store.directory).list("artifacts") == []


async def test_generated_shell_cannot_choose_a_broader_sandbox_root(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    result = await BashTool().execute(
        BashToolInput(command="touch escaped", cwd=str(tmp_path)),
        ToolExecutionContext(cwd=workspace),
    )
    assert result.is_error and result.no_effect is True
    assert not (tmp_path / "escaped").exists()
