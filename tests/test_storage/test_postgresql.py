"""These tests require a disposable, migrated PostgreSQL database, never SQLite."""

import asyncio
import os
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from researchx.state.errors import ResearchError
from researchx.state.models import ResearchMemory, SourceRecord, now
from researchx.storage import schema as s
from researchx.storage.content import LocalContentStore
from researchx.storage.database import Database, DatabaseConfigurationError, workspace_id, bind_database
from researchx.storage.research_records import ResearchRecords, scope


@pytest.fixture
async def database():
    url = os.environ["RESEARCHX_TEST_DATABASE_URL"]
    database = Database(url)
    await database.check()
    try:
        yield database
    finally:
        await database.close()


@pytest.fixture
def records(database, tmp_path):
    return ResearchRecords(database, workspace_id(tmp_path), uuid4().hex[:12], str(tmp_path))


def validate(memory):
    ResearchMemory.model_validate(memory.model_dump())


async def test_transaction_rolls_back_entities_revision_receipt_and_history(records):
    async with records.transaction() as db:
        assert (await records.load(db)).revision == 0
    with pytest.raises(RuntimeError, match="crash"):
        async with records.transaction() as db:
            memory = await records.load(db)
            memory.revision = 1
            memory.history.append({"revision": 1, "action": "test", "at": now()})
            memory.operations["op"] = {"fingerprint": "a" * 64, "receipt": {"revision": 1}}
            await records.save(db, memory, validate, expected_revision=0)
            raise RuntimeError("simulated crash")
    async with records.transaction() as db:
        assert (await records.load(db)) == ResearchMemory(session_id=records.session)


async def test_concurrent_revision_has_exactly_one_winner(records):
    async def write():
        try:
            async with records.transaction() as db:
                memory = await records.load(db)
                memory.revision = 1
                await records.save(db, memory, validate, expected_revision=0)
            return "committed"
        except ResearchError:
            return "conflict"

    assert sorted(await asyncio.gather(write(), write())) == ["committed", "conflict"]
    async with records.transaction() as db:
        assert (await records.load(db)).revision == 1


async def test_receipt_immutable_and_content_verified_before_reference(records, tmp_path):
    content = LocalContentStore(tmp_path / "objects")
    reference = await content.put(records.workspace, "研究来源正文".encode())
    async with records.transaction() as db:
        await records.register_content(db, content, [reference])
        memory = await records.load(db)
        memory.sources["src_test"] = SourceRecord(
            id="src_test", kind="user", title="来源", locator="user:test", origin_id="test",
            collected_at=now(), content_hash=reference.content_hash,
            snapshot=f"content/{reference.content_hash}.txt",
        )
        memory.revision = 1
        memory.operations["same"] = {"fingerprint": "a" * 64, "receipt": {"revision": 1}}
        memory.history.append({"revision": 1, "action": "capture", "at": now()})
        await records.save(db, memory, validate, expected_revision=0)
    async with records.transaction() as db:
        assert await records.load(db) == memory
    with pytest.raises(ResearchError, match="different content"):
        async with records.transaction() as db:
            changed = await records.load(db)
            changed.revision = 2
            changed.operations["same"]["fingerprint"] = "b" * 64
            await records.save(db, changed, validate, expected_revision=1)
    async with records.transaction() as db:
        assert await records.load(db) == memory
    path = content.root / reference.object_key
    path.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="hash mismatch"):
        async with records.transaction() as db:
            await records.register_content(db, content, [reference])


async def test_missing_content_and_cross_workspace_reference_rejected(records, database, tmp_path):
    other = ResearchRecords(database, workspace_id(tmp_path / "other"), records.session,
                            str(tmp_path / "other"))
    async with other.transaction() as db:
        assert not (await other.load(db)).sources
    with pytest.raises(IntegrityError):
        async with records.transaction() as db:
            await db.execute(s.entities["sources"].insert().values(
                workspace_id=records.workspace, session_id=records.session, record_id="missing",
                position=0, content_hash="f" * 64, payload={},
            ))
    async with other.transaction() as db:
        assert not (await db.scalars(select(s.entities["sources"].c.record_id).where(
            scope(s.entities["sources"], other.workspace, other.session)
        ))).all()


def test_database_requires_postgresql_without_echoing_credentials():
    with pytest.raises(DatabaseConfigurationError) as error:
        Database("sqlite:///password-secret")
    assert "password-secret" not in str(error.value)


async def test_content_scope_and_symlink_boundaries(tmp_path):
    content = LocalContentStore(tmp_path / "content")
    workspace = "a" * 16
    (content.root / workspace).symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinks"):
        await content.put(workspace, b"private")
    with pytest.raises(ValueError, match="workspace"):
        await content.put("../escape", b"private")


async def test_research_store_uses_postgresql_and_preserves_replay(database, tmp_path, monkeypatch):
    from researchx.state.store import ResearchStore

    monkeypatch.setenv("RESEARCHX_DATA_DIR", str(tmp_path / "data"))
    with bind_database(database):
        store = ResearchStore(tmp_path, uuid4().hex[:12])
        source = await store.capture(origin_id="user", kind="user", content="研究目标")
        command = {"action": "set_context", "operation_id": "context", "expected_revision": 1,
                   "goal": "研究目标", "user_source_ids": [source.id]}
        first = await store.apply(command)
        replay = await store.apply(command)
        assert replay == {**first, "current_revision": 2}
        assert (await store.load()).revision == 2
        assert await store.read_source(source) == "研究目标"
        assert not (store.directory / "state.json").exists()
        with pytest.raises(ResearchError, match="different content"):
            await store.apply({**command, "goal": "另一个目标"})


async def test_web_cli_messages_and_files_roundtrip(database, tmp_path, monkeypatch):
    from researchx.web.storage import WebSessionBackend
    from researchx.services.sessions.storage import save_session_snapshot, load_session_by_id
    from researchx.engine.messages import ConversationMessage, TextBlock
    from researchx.api.usage import UsageSnapshot
    from researchx.workspace.session_files import SessionFiles
    from researchx.state.store import ResearchStore

    monkeypatch.setenv("RESEARCHX_DATA_DIR", str(tmp_path / "data"))
    with bind_database(database):
        backend = WebSessionBackend(str(tmp_path))
        record = await backend.create("test")
        messages = [ConversationMessage(role="user", content=[TextBlock(text="中文会话")])]
        await backend.save_snapshot(cwd=tmp_path, model="test", system_prompt="系统提示",
                                    messages=messages, usage=UsageSnapshot(), session_id=record["session_id"])
        loaded = await backend.load_by_id(tmp_path, record["session_id"])
        assert loaded["messages"] == [m.model_dump(mode="json") for m in messages]
        assert (await backend.list_snapshots(tmp_path))[0]["session_id"] == record["session_id"]
        assert await WebSessionBackend(str(tmp_path / "other")).load_by_id(tmp_path / "other", record["session_id"]) is None
        cli_id = uuid4().hex[:12]
        await save_session_snapshot(cwd=tmp_path, model="test", system_prompt="系统提示", messages=messages,
                                    usage=UsageSnapshot(), session_id=cli_id)
        assert (await load_session_by_id(tmp_path, cli_id))["messages"] == loaded["messages"]
        files = SessionFiles(ResearchStore(tmp_path, record["session_id"]))
        upload = await files.upload("来源.md", "来源正文".encode())
        assert (await files.attachment(upload["id"]))[0] == upload
        path = tmp_path / "report.md"
        path.write_text("报告内容")
        artifact = await files.register(path, task_id=None, status="draft", kind="report")
        assert (await files.artifact(artifact["id"]))[1].read_text() == "报告内容"
        await backend.delete(record["session_id"])
        assert await backend.load_by_id(tmp_path, record["session_id"]) is None
        assert not list((tmp_path / "data").rglob("state.json"))


async def test_ledger_claim_once_recovery_and_scope(database, tmp_path):
    from researchx.services.execution.operations import OperationStore

    ledger = OperationStore(tmp_path, database=database)
    session, run = uuid4().hex, uuid4().hex
    values = dict(session=session, scope=str(tmp_path), run=run, call="write", tool="test",
                  version="1", digest="a" * 64, effect="external_write",
                  resources={"read": [], "write": ["*"]})
    row = await ledger.prepare(**values)
    assert (await ledger.prepare(**values))["operation_id"] == row["operation_id"]
    with pytest.raises(ValueError, match="different input"):
        await ledger.prepare(**{**values, "digest": "b" * 64})
    attempts = await asyncio.gather(ledger.claim(row["operation_id"], "owner"),
                                   ledger.claim(row["operation_id"], "other"))
    assert sum(attempt is not None for attempt in attempts) == 1
    current = await ledger.get(row["operation_id"], session=session, scope=str(tmp_path))
    await ledger.settle(row["operation_id"], "succeeded", owner=current["owner"])
    await ledger.settle(row["operation_id"], "succeeded", owner=current["owner"])
    assert await ledger.claim(row["operation_id"], "replay") is None
    assert not await ledger.recover(session=session, scope=str(tmp_path))
    other = OperationStore(tmp_path / "other", database=database)
    with pytest.raises(ValueError, match="Unknown operation"):
        await other.get(row["operation_id"], session=session, scope=str(tmp_path))


async def test_session_control_isolation_preserves_external_resource_locks(database, tmp_path):
    from researchx.services.execution.operations import OperationStore
    store = OperationStore(tmp_path, database=database)

    async def prepare(session, call, tool, resources, effect="local_write"):
        return await store.prepare(session=session, scope=str(tmp_path), run=session, call=call,
                                   tool=tool, version="1", digest=call, effect=effect, resources=resources)
    interrupted = await prepare("a", "control", "research_memory", {"read": [], "write": ["research.control"]})
    await store.claim(interrupted["operation_id"], "owner")
    await store.settle(interrupted["operation_id"], "uncertain", owner="owner")
    unrelated = await prepare("b", "read", "read_file", {"read": [], "write": ["*"]}, "read_only")
    assert await store.unresolved_conflicts(unrelated["operation_id"]) == []
    assert await store.claim(unrelated["operation_id"], "other")
    await store.settle(unrelated["operation_id"], "succeeded", owner="other")
    external = await prepare("b", "post", "external_post", {"read": [], "write": ["*"]}, "external_write")
    assert await store.claim(external["operation_id"], "remote")
    await store.settle(external["operation_id"], "uncertain", owner="remote")
    conflicting = await prepare("c", "post2", "external_post", {"read": [], "write": ["*"]}, "external_write")
    assert await store.unresolved_conflicts(conflicting["operation_id"]) == [external["operation_id"]]
    assert await store.claim(conflicting["operation_id"], "must-not-run") is None


async def test_context_archive_survives_projection_removal_and_keeps_parent(postgres_scope, tmp_path):
    from researchx.engine.messages import ConversationMessage
    from researchx.services.context.snapshots import save_context_snapshot
    from researchx.state.store import ResearchStore
    from researchx.storage.conversations import ConversationRecords
    store = ResearchStore(tmp_path, "a" * 12)
    path = await save_context_snapshot([ConversationMessage.from_user_text("原始上下文")],
                                       model="fixture", metadata={"research_store": store})
    path.unlink()  # Backup projection is not authoritative.
    records = ConversationRecords(postgres_scope, str(tmp_path))
    archives = await records.list(channel="context_archive")
    assert len(archives) == 1 and archives[0]["parent_session_id"] == "a" * 12
    assert archives[0]["messages"][0]["content"][0]["text"] == "原始上下文"
    foreign = ConversationRecords(postgres_scope, str(tmp_path / "foreign"))
    assert await foreign.load(archives[0]["session_id"]) is None


async def test_mvcc_read_sees_complete_old_or_new_revision_without_writer_lock(records):
    from researchx.state.models import TaskContext
    async with records.transaction() as db:
        assert (await records.load(db)).revision == 0
    changed, release = asyncio.Event(), asyncio.Event()

    async def writer():
        async with records.transaction() as db:
            memory = await records.load(db)
            context = TaskContext(goal="原子更新目标")
            memory.task_context.append(context)
            memory.current_context_id = context.id
            memory.revision = 1
            memory.history.append({"revision": 1, "action": "set_context", "at": now()})
            await records.save(db, memory, validate, expected_revision=0)
            changed.set()
            await release.wait()

    task = asyncio.create_task(writer())
    await asyncio.wait_for(changed.wait(), 5)
    try:
        async with records.database.transaction() as db:
            old = await asyncio.wait_for(records.load(db), 5)
        assert old.revision == 0 and old.task_context == [] and old.history == []
    finally:
        release.set()
        await task
    async with records.database.transaction() as db:
        new = await records.load(db)
    assert new.revision == 1 and new.task_context[0].id == new.current_context_id
    assert new.history[0]["revision"] == 1


async def test_report_body_in_history_uses_content_store_and_preserves_domain_view(records):
    body = "报告正文\n" * 500
    async with records.transaction() as db:
        memory = await records.load(db)
        memory.revision = 1
        memory.history.append({"revision": 1, "action": "submit_artifact", "at": now(),
                               "data": {"content": body, "kind": "report_draft"}})
        await records.save(db, memory, validate, expected_revision=0)
    async with records.database.transaction() as db:
        stored = await db.scalar(select(s.research_history.c.payload).where(
            scope(s.research_history, records.workspace, records.session)))
        assert set(stored["data"]["content"]) == {"content_ref"}
        assert body not in str(stored)
        assert await records.load(db) == memory


@pytest.mark.parametrize("failure", [asyncio.TimeoutError, asyncio.CancelledError])
async def test_database_health_timeout_is_safe_and_cancellation_propagates(database, monkeypatch, failure):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    @asynccontextmanager
    async def connection():
        raise failure()
        yield
    with monkeypatch.context() as patch:
        patch.setattr(database, "engine", SimpleNamespace(connect=connection))
        expected = DatabaseConfigurationError if failure is asyncio.TimeoutError else asyncio.CancelledError
        with pytest.raises(expected):
            await database.check()


async def test_conversation_images_and_large_blocks_use_verified_content_objects(database, tmp_path):
    from researchx.storage.conversations import ConversationRecords
    from researchx.engine.messages import ConversationMessage, ImageBlock, TextBlock
    records = ConversationRecords(database, str(tmp_path))
    message = ConversationMessage(role="user", content=[
        ImageBlock(media_type="image/png", data="original-base64-encoding", source_path="original.png"),
        TextBlock(text="长正文" * 30000),
    ]).model_dump(mode="json")
    await records.write({"session_id": "images", "messages": [message]}, channel="cli")
    async with database.transaction() as db:
        raw = await db.scalar(select(s.conversation_messages.c.payload).where(
            scope(s.conversation_messages, records.workspace, "images")))
        assert "data" not in raw["content"][0] and raw["content"][0]["data_ref"].startswith("sha256:")
        assert "text" not in raw["content"][1] and raw["content"][1]["text_ref"].startswith("sha256:")
    assert (await records.load("images"))["messages"] == [message]
    # A reference cannot resolve in a different workspace, even if copied in a corrupted row.
    foreign = ConversationRecords(database, str(tmp_path / "other"))
    await foreign.write({"session_id": "images", "messages": []}, channel="cli")
    async with database.transaction() as db:
        await db.execute(s.conversation_messages.insert().values(
            workspace_id=foreign.workspace, session_id="images", sequence=0, payload=raw))
    with pytest.raises(FileNotFoundError, match="reference"):
        await foreign.load("images")
