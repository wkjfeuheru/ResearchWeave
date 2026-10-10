"""Real PostgreSQL import transactions; SQLite is only legacy fixture input."""

import hashlib
import json
import sqlite3
from uuid import uuid4

from sqlalchemy import select

from researchx.state.models import ResearchMemory, SourceRecord, now
from researchx.state.store import ResearchStore
from researchx.storage import schema as s
from researchx.storage.database import workspace_id
from researchx.storage.legacy_import import import_legacy
from researchx.storage.research_records import ResearchRecords


def legacy_tree(tmp_path):
    root = tmp_path / "legacy"
    cwd = tmp_path / "project"
    session = uuid4().hex[:12]
    digest = workspace_id(cwd)
    directory = root / "research" / digest / session
    (directory / "content").mkdir(parents=True)
    body = "旧研究来源正文".encode()
    content_hash = hashlib.sha256(body).hexdigest()
    (directory / "content" / f"{content_hash}.txt").write_bytes(body)
    source = SourceRecord(id="src_legacy", kind="user", title="旧来源", locator="user:legacy",
                          origin_id="legacy", collected_at=now(), content_hash=content_hash,
                          snapshot=f"content/{content_hash}.txt")
    memory = ResearchMemory(session_id=session, revision=1, sources={source.id: source},
                            history=[{"revision": 1, "action": "capture_source", "at": now(), "data": {}}])
    state = directory / "state.json"
    state.write_text(memory.model_dump_json(), encoding="utf-8")
    web = root / "web" / "sessions" / digest
    web.mkdir(parents=True)
    conversation = {"session_id": session, "cwd": str(cwd.resolve()), "source": "web",
                    "profile_id": "test", "model": "test", "summary": "旧会话", "created_at": 1,
                    "updated_at": 2, "messages": [], "message_count": 0, "usage": {}, "tool_metadata": {}}
    (web / f"{session}.json").write_text(json.dumps(conversation), encoding="utf-8")
    return root, cwd, session, memory, state


async def test_dry_run_import_replay_reload_and_source_preservation(postgres_scope, tmp_path):
    root, cwd, session, memory, state = legacy_tree(tmp_path)
    before = {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    dry = await import_legacy(root, [cwd])
    assert not dry["errors"] and dry["dry_run"]
    assert dry["counts"]["research"]["discovered"] == 1
    assert dry["counts"]["research"]["imported"] == 0
    first = await import_legacy(root, [cwd], postgres_scope)
    assert not first["errors"]
    assert first["counts"]["research"]["imported"] == 1
    assert first["counts"]["web"]["imported"] == 1
    store = ResearchStore(cwd, session)
    assert await store.load() == memory
    assert await store.read_source(memory.sources["src_legacy"]) == "旧研究来源正文"
    second = await import_legacy(root, [cwd], postgres_scope)
    assert not second["errors"]
    assert second["counts"]["research"]["skipped"] == second["counts"]["web"]["skipped"] == 1
    assert {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()} == before
    state.write_text(memory.model_copy(update={"revision": 2}).model_dump_json())
    changed = await import_legacy(root, [cwd], postgres_scope)
    assert changed["counts"]["research"]["failed"] == 1
    assert (await store.load()).revision == 1


async def test_corruption_is_diagnosed_and_failed_file_leaves_no_import_receipt(postgres_scope, tmp_path):
    root, cwd, session, memory, state = legacy_tree(tmp_path)
    (state.parent / memory.sources["src_legacy"].snapshot).unlink()
    report = await import_legacy(root, [cwd], postgres_scope)
    assert report["counts"]["research"]["failed"] == 1
    assert any("missing" in error["error"] for error in report["errors"])
    async with postgres_scope.transaction() as db:
        assert await db.scalar(select(s.legacy_import_records.c.source_path).where(
            s.legacy_import_records.c.source_path == str(state)
        )) is None


async def test_import_transaction_rolls_back_state_and_ledger(postgres_scope, tmp_path, monkeypatch):
    root, cwd, session, memory, state = legacy_tree(tmp_path)
    original = ResearchRecords.save

    async def interrupted(*args, **kwargs):
        await original(*args, **kwargs)
        raise OSError("simulated import interruption")

    monkeypatch.setattr(ResearchRecords, "save", interrupted)
    failed = await import_legacy(root, [cwd], postgres_scope)
    assert failed["counts"]["research"]["failed"] == 1
    assert (await ResearchStore(cwd, session).load()).revision == 0
    monkeypatch.setattr(ResearchRecords, "save", original)
    recovered = await import_legacy(root, [cwd], postgres_scope)
    assert not recovered["errors"]
    assert (await ResearchStore(cwd, session).load()) == memory


async def test_sqlite_legacy_copy_preserves_unresolved_writes_and_unknown_usage(postgres_scope, tmp_path):
    root = tmp_path / "legacy"
    root.mkdir()
    cwd = tmp_path / "cwd"
    path = root / "operations.sqlite3"
    run, operation, attempt = uuid4().hex, uuid4().hex, uuid4().hex
    connection = sqlite3.connect(path)
    for name, table in (("runs", s.tool_runs), ("steps", s.tool_steps), ("operations", s.tool_operations),
                        ("attempts", s.tool_attempts), ("api_attempts", s.api_attempts)):
        columns = [column for column in table.c if column.name not in {"workspace_id", "host_id"}]
        connection.execute(f"CREATE TABLE {name} (" + ",".join(
            f'{column.name} ' + ("INTEGER" if column.name in {"attempts", "pid", "number"} else
                                "REAL" if column.name in {"created", "updated", "lease_until"} else "TEXT")
            for column in columns) + ")")
    connection.execute("INSERT INTO runs VALUES(?,?,?,?,?,?)", (run, "legacy", str(cwd), "running", None, 1))
    connection.execute("INSERT INTO steps VALUES(?,?,?,?)", (run, "call", "running", 1))
    values = dict(operation_id=operation, run_id=run, session_id="legacy", scope=str(cwd), step_id="call",
                  call_id="call", tool="post", contract_version="1", input_digest="a" * 64,
                  effect="external_write", status="running", attempts=1, owner="old-owner", pid=999999,
                  created=1, updated=1, idempotency_key=operation, resources=json.dumps({"read": [], "write": ["*"]}))
    connection.execute("INSERT INTO operations (" + ",".join(values) + ") VALUES (" + ",".join("?" for _ in values) + ")", tuple(values.values()))
    connection.execute("INSERT INTO attempts VALUES(?,?,?,?,?,?)", (attempt, operation, 1, "running", 1, 1))
    record = {"attempt_id": uuid4().hex, "request_id": uuid4().hex, "status": "failed", "usage_status": "unknown", "usage": None, "started": 1}
    connection.execute("INSERT INTO api_attempts VALUES(?,?,?,?,?,?)", (record["attempt_id"], "legacy", record["request_id"], json.dumps(record), "failed", 1))
    connection.commit()
    connection.close()
    before = path.read_bytes()
    report = await import_legacy(root, [cwd], postgres_scope)
    assert not report["errors"]
    assert report["counts"]["ledger"]["imported"] == 1
    async with postgres_scope.transaction() as db:
        assert await db.scalar(select(s.tool_operations.c.status).where(s.tool_operations.c.operation_id == hashlib.sha256(json.dumps([workspace_id(cwd), "legacy", str(cwd), "call"]).encode()).hexdigest())) == "uncertain"
        assert await db.scalar(select(s.tool_attempts.c.status).where(s.tool_attempts.c.attempt_id == attempt)) == "uncertain"
        assert (await db.scalar(select(s.api_attempts.c.record).where(s.api_attempts.c.attempt_id == record["attempt_id"]))) == record
    assert path.read_bytes() == before
    assert (await import_legacy(root, [cwd], postgres_scope))["counts"]["ledger"]["skipped"] == 1


async def test_context_archives_and_large_tool_content_import(postgres_scope, tmp_path):
    from researchx.storage.conversations import ConversationRecords
    from researchx.storage.content import read_object
    root, cwd, session, _, _ = legacy_tree(tmp_path)
    archives = root / "context_snapshots"
    archives.mkdir()
    archive = archives / "old.json"
    archive.write_text(json.dumps({"version": 1, "session_id": session, "model": "fixture",
                                   "messages": [{"role": "user", "content": [{"type": "text", "text": "压缩前的历史"}]}],
                                   "metadata": {}}))
    artifacts = root / "tool_artifacts"
    artifacts.mkdir()
    (artifacts / "large.txt").write_text("保留的大工具输出")
    before = archive.read_bytes()
    report = await import_legacy(root, [cwd], postgres_scope)
    assert not report["errors"]
    assert report["counts"]["context_archive"]["imported"] == 1
    assert report["counts"]["tool_content"]["imported"] == 1
    item = next(record for record in report["records"] if record["kind"] == "context_archive")
    restored = await ConversationRecords(postgres_scope, cwd).load(item["session"])
    assert restored["parent_session_id"] == session
    assert restored["messages"][0]["content"][0]["text"] == "压缩前的历史"
    async with postgres_scope.transaction() as db:
        digest = await db.scalar(select(s.file_records.c.content_hash).where(
            s.file_records.c.workspace_id == workspace_id(cwd), s.file_records.c.kind == "tool_content"))
    assert (await read_object(postgres_scope, workspace_id(cwd), "sha256:" + digest)).decode() == "保留的大工具输出"
    again = await import_legacy(root, [cwd], postgres_scope)
    assert not again["errors"] and again["counts"]["context_archive"]["skipped"] == 1
    assert again["counts"]["tool_content"]["skipped"] == 1
    assert archive.read_bytes() == before
