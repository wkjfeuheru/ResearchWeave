"""Explicit, read-only legacy discovery/import. Never imported by request handlers."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sqlite3
import time
import shutil
from tempfile import TemporaryDirectory
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError
from pydantic import ValidationError
from sqlalchemy.dialects.postgresql import insert

from researchx.state.models import ResearchMemory, SourceRecord, ResearchArtifact
from researchx.state.store import ResearchStore
from researchx.storage import schema as s
from researchx.storage.content import content_store, persist_object
from researchx.storage.conversations import ConversationRecords, subagent_session_id
from researchx.storage.database import Database, bind_database, workspace_id
from researchx.storage.filesystem import atomic_write_text
from researchx.storage.research_records import ResearchRecords, ensure_session, scope


@dataclass
class LegacyInput:
    path: Path
    kind: str
    payload: Any
    fingerprint: str
    cwd: str | None = None
    session: str | None = None
    content_hashes: dict[str, str] = field(default_factory=dict)


def _safe_file(root: Path, path: Path) -> Path:
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Legacy reference leaves the approved source root")
    for part in (path, *path.parents):
        if part == root:
            break
        if part.is_symlink():
            raise ValueError("Legacy input must not traverse symlinks")
    if not path.is_file():
        raise ValueError("Referenced content file is missing")
    return path


def _json(root: Path, path: Path) -> Any:
    return json.loads(_safe_file(root, path).read_text(encoding="utf-8"))


def _sqlite(root: Path, path: Path) -> dict[str, list[dict[str, Any]]]:
    _safe_file(root, path)
    # SQLite may maintain SHM even with mode=ro. Read a verified private copy of all
    # sidecars, so discovery never modifies the source ledger or checkpoints its WAL.
    with TemporaryDirectory(prefix="researchx-legacy-ledger-") as temporary:
        copied = Path(temporary) / path.name
        hashes = {}
        for suffix in ("", "-wal", "-shm"):
            original = Path(str(path) + suffix)
            if original.exists():
                _safe_file(root, original)
                hashes[suffix] = hashlib.sha256(original.read_bytes()).hexdigest()
                shutil.copyfile(original, Path(str(copied) + suffix))
        for suffix, digest in hashes.items():
            if hashlib.sha256(Path(str(path) + suffix).read_bytes()).hexdigest() != digest:
                raise ValueError(
                    "Legacy ledger changed during copy; stop the old application before importing"
                )
        return _read_ledger_copy(copied)


def _read_ledger_copy(path: Path) -> dict[str, list[dict[str, Any]]]:
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        names = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        result = {}
        for table in ("runs", "steps", "operations", "attempts", "api_attempts"):
            if table not in names:
                raise ValueError(f"Legacy ledger is missing required table: {table}")
            result[table] = [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]
        return result
    finally:
        connection.close()


def _diagnostic(error: Exception) -> str:
    if isinstance(error, ValidationError):
        return "Invalid schema fields: " + ", ".join(
            ".".join(map(str, item["loc"])) + " (" + item["type"] + ")"
            for item in error.errors(include_input=False)
        )
    if isinstance(error, SQLAlchemyError):
        return "PostgreSQL import failed: " + type(error).__name__
    return str(error)


def _normalize_research(payload: dict[str, Any]) -> list[str]:
    """Explicit schema-1 answer upgrade, leaving the legacy source file untouched."""
    from researchx.state.models import SourceDisplay

    changes = []
    for identity, answer in payload.get("answers", {}).items():
        if "answer_id" not in answer:
            answer["answer_id"] = identity
            changes.append("answer_id_from_record_key")
        for citation in answer.get("citations", {}).values():
            source = citation.get("source", {})
            extra = set(source) - set(SourceDisplay.__annotations__)
            if extra and extra <= (set(SourceRecord.model_fields) - {"snapshot"}):
                # Frozen display fields retain their old values; the source entity carries provenance.
                citation["source"] = {
                    key: source[key] for key in SourceDisplay.__annotations__ if key in source
                }
                changes.append("legacy_source_display_projection")
    return sorted(set(changes))


def discover(root: Path, workspaces: list[Path]) -> tuple[list[LegacyInput], dict[str, Any]]:
    root = root.absolute()
    report: dict[str, Any] = {
        "source_root": str(root),
        "dry_run": True,
        "counts": {},
        "records": [],
        "errors": [],
    }
    inputs: list[LegacyInput] = []
    if not root.is_dir() or root.is_symlink():
        report["errors"].append(
            {"path": str(root), "error": "Source root must be an existing real directory"}
        )
        return inputs, report
    mapping = {workspace_id(path): str(path.resolve()) for path in workspaces}
    session_scopes: dict[str, set[str]] = {}
    groups = (
        ("web", sorted((root / "web" / "sessions").glob("*/*.json"))),
        (
            "cli",
            sorted((root / "sessions").rglob("session-*.json"))
            + sorted((root / "sessions").rglob("latest.json")),
        ),
        ("research", sorted((root / "research").rglob("state.json"))),
        ("files", sorted((root / "research").rglob("manifest.json"))),
        ("dispatch", sorted((root / "research").rglob("batch.json"))),
        ("subagent", sorted((root / "research").glob("*/*/dispatches/*/*/messages.json"))),
        ("context_archive", sorted((root / "context_snapshots").glob("*.json"))),
        ("tool_content", sorted((root / "tool_artifacts").glob("*"))),
        ("ledger", sorted(root.rglob("operations.sqlite3"))),
    )
    for kind, paths in groups:
        report["counts"][kind] = {
            "discovered": len(paths),
            "imported": 0,
            "skipped": 0,
            "failed": 0,
        }
        for path in paths:
            try:
                payload: Any = (
                    _sqlite(root, path)
                    if kind == "ledger"
                    else {}
                    if kind == "tool_content"
                    else _json(root, path)
                )
                fingerprint = hashlib.sha256(
                    json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
                ).hexdigest()
                item = LegacyInput(path, kind, payload, fingerprint)
                if kind in {"web", "cli"}:
                    if not isinstance(payload["cwd"], str) or not isinstance(
                        payload["session_id"], str
                    ):
                        raise ValueError("Invalid conversation identity")
                    item.cwd = str(Path(payload["cwd"]).resolve())
                    item.session = payload["session_id"]
                    assert item.session is not None
                    mapping[workspace_id(item.cwd)] = item.cwd
                    session_scopes.setdefault(item.session, set()).add(item.cwd)
                elif kind == "research":
                    changes = _normalize_research(payload)
                    memory = ResearchMemory.model_validate(payload)
                    item.session = memory.session_id
                    if path.parent.name != memory.session_id:
                        raise ValueError("Research session ID differs from source directory")
                    item.cwd = mapping.get(path.parent.parent.name)
                    if item.cwd is None:
                        raise ValueError(
                            "Workspace mapping missing; supply --workspace with the original canonical path"
                        )
                    session_scopes.setdefault(item.session, set()).add(item.cwd)
                    validator = object.__new__(ResearchStore)
                    validator._validate(memory)
                    content_records: list[SourceRecord | ResearchArtifact] = [
                        *memory.sources.values(),
                        *memory.artifacts.values(),
                    ]
                    for record in content_records:
                        body = _safe_file(root, path.parent / record.snapshot).read_bytes()
                        if hashlib.sha256(body).hexdigest() != record.content_hash:
                            raise ValueError("Research snapshot hash mismatch")
                elif kind in {"files", "dispatch", "subagent"}:
                    # research/<workspace>/<session>/{files|dispatches}/...
                    relative = path.relative_to(root / "research").parts
                    if len(relative) < 4:
                        raise ValueError("Unrecognized session resource layout")
                    item.cwd, item.session = mapping.get(relative[0]), relative[1]
                    if item.cwd is None:
                        raise ValueError("Session resource has no workspace mapping")
                    if kind == "subagent":
                        if not isinstance(payload, list):
                            raise ValueError("Legacy child transcript must be a message list")
                        parent = item.session
                        item.session = subagent_session_id(parent, path)
                        item.payload = {
                            "session_id": item.session,
                            "parent_session_id": parent,
                            "cwd": item.cwd,
                            "model": "legacy",
                            "messages": payload,
                        }
                    elif kind == "files":
                        filename = payload.get("original") or payload.get("filename")
                        if not isinstance(filename, str):
                            raise ValueError("File manifest has no content filename")
                        body = _safe_file(root, path.parent / filename).read_bytes()
                        digest = hashlib.sha256(body).hexdigest()
                        if payload.get("document_hash") and digest != payload["document_hash"]:
                            raise ValueError("Attachment content hash mismatch")
                        item.content_hashes[str(path.parent / filename)] = digest
                        item.fingerprint = hashlib.sha256(
                            (fingerprint + digest).encode()
                        ).hexdigest()
                        if payload.get("id") != path.parent.name:
                            raise ValueError("File manifest identity mismatch")
                    else:
                        if payload.get("schema_version", 1) not in {1, 2}:
                            raise ValueError("Unsupported dispatch audit schema")
                        results = {
                            result["task_id"]: result for result in payload.get("results", [])
                        }
                        for child in sorted(path.parent.glob("*/result.json")):
                            result = _json(root, child)
                            if result.get("task_id") != child.parent.name:
                                raise ValueError("Dispatch child identity mismatch")
                            results[result["task_id"]] = result
                        payload["results"] = list(results.values())
                        item.fingerprint = hashlib.sha256(
                            json.dumps(payload, sort_keys=True).encode()
                        ).hexdigest()
                elif kind in {"context_archive", "tool_content"}:
                    parent = payload.get("session_id") or "legacy-unattributed"
                    candidates = session_scopes.get(parent, set())
                    if len(candidates) == 1:
                        item.cwd = next(iter(candidates))
                    elif not candidates and len(workspaces) == 1:
                        item.cwd = str(workspaces[0].resolve())
                    else:
                        raise ValueError(
                            "Archive workspace is ambiguous; supply one explicit --workspace"
                        )
                    item.session = (
                        "archive-" + hashlib.sha256(str(path).encode()).hexdigest()[:32]
                        if kind == "context_archive"
                        else parent
                    )
                    body = _safe_file(root, path).read_bytes()
                    digest = hashlib.sha256(body).hexdigest()
                    item.content_hashes[str(path)] = digest
                    item.fingerprint = digest
                    if kind == "context_archive":
                        from researchx.engine.messages import ConversationMessage

                        if payload.get("version") != 1 or not isinstance(
                            payload.get("messages"), list
                        ):
                            raise ValueError("Unsupported context archive schema")
                        for message in payload["messages"]:
                            ConversationMessage.model_validate(message)
                        item.payload = {
                            "session_id": item.session,
                            "parent_session_id": parent,
                            "cwd": item.cwd,
                            "model": payload.get("model", ""),
                            "messages": payload["messages"],
                            "tool_metadata": payload.get("metadata", {}),
                        }
                    else:
                        item.payload = {
                            "id": "legacy-" + hashlib.sha256(str(path).encode()).hexdigest(),
                            "filename": path.name,
                            "legacy_source": str(path),
                        }
                else:
                    relative = path.relative_to(root).parts
                    if relative[0] == "research" and len(relative) >= 4:
                        item.cwd, item.session = mapping.get(relative[1]), relative[2]
                    elif len(workspaces) == 1:
                        item.cwd = str(workspaces[0].resolve())
                    if item.cwd is None:
                        raise ValueError(
                            "Ledger workspace mapping missing; supply a single --workspace for unscoped API history"
                        )
                    item.session = item.session or "legacy-unattributed"
                    for operation in payload["operations"]:
                        if operation.get("result_ref"):
                            content_path = _safe_file(root, Path(operation["result_ref"]))
                            item.content_hashes[str(content_path)] = hashlib.sha256(
                                content_path.read_bytes()
                            ).hexdigest()
                    item.fingerprint = hashlib.sha256(
                        (fingerprint + json.dumps(item.content_hashes, sort_keys=True)).encode()
                    ).hexdigest()
                inputs.append(item)
                stats: dict[str, Any] = {
                    "path": str(path),
                    "kind": kind,
                    "workspace": item.cwd,
                    "session": item.session,
                }
                stats["content_hashes"] = dict(item.content_hashes)
                if kind == "research":
                    stats["content_hashes"].update(
                        {record.snapshot: record.content_hash for record in content_records}
                    )
                    stats["normalizations"] = changes
                    stats["revision"] = payload["revision"]
                    stats["entities"] = {
                        key: len(payload.get(key, []))
                        for key in (*s.ENTITY_FIELDS, "operations", "history")
                        if key != "project"
                    }
                if kind in {"web", "cli", "subagent", "context_archive"}:
                    stats["messages"] = len(item.payload.get("messages", []))
                if kind == "ledger":
                    stats["tables"] = {name: len(rows) for name, rows in payload.items()}
                    stats["operation_states"] = dict(
                        Counter(row["status"] for row in payload["operations"])
                    )
                report["records"].append(stats)
            except (ValueError, KeyError, OSError, sqlite3.Error) as exc:
                report["counts"][kind]["failed"] += 1
                report["errors"].append(
                    {"path": str(path), "kind": kind, "error": _diagnostic(exc)}
                )
    report["totals"] = {
        "workspaces": len({item.cwd for item in inputs if item.cwd}),
        "sessions": len({(item.cwd, item.session) for item in inputs if item.session}),
        "messages_in_input_files": sum(
            len(item.payload.get("messages", []))
            for item in inputs
            if item.kind in {"web", "cli", "subagent", "context_archive"}
        ),
    }
    return inputs, report


async def _research(item: LegacyInput, db: Database) -> None:
    assert item.cwd is not None and item.session is not None
    memory = ResearchMemory.model_validate(item.payload)
    records = ResearchRecords(db, workspace_id(item.cwd), item.session, item.cwd)
    validator = object.__new__(ResearchStore)
    validator._validate(memory)
    content = content_store()
    references = []
    content_records: list[SourceRecord | ResearchArtifact] = [
        *memory.sources.values(),
        *memory.artifacts.values(),
    ]
    for record in content_records:
        body = await asyncio.to_thread((item.path.parent / record.snapshot).read_bytes)
        reference = await content.put(records.workspace, body)
        if reference.content_hash != record.content_hash:
            raise ValueError("Snapshot changed during import")
        references.append(reference)
    async with records.transaction() as transaction:
        current = await records.load(transaction)
        if current == memory:
            return
        if current != ResearchMemory(session_id=item.session):
            raise ValueError("Existing PostgreSQL research state differs; refusing overwrite")
        await records.register_content(transaction, content, references)
        if memory.revision == 0:
            raise ValueError("Nonempty legacy state cannot have revision zero")
        # Import-only initial revision positioning, while the empty header is locked.
        await transaction.execute(
            update(s.research_sessions)
            .where(scope(s.research_sessions, records.workspace, records.session))
            .values(revision=memory.revision - 1)
        )
        await records.save(
            transaction, memory, validator._validate, expected_revision=memory.revision - 1
        )
        loaded = await records.load(transaction)
        validator._validate(loaded)
        if loaded != memory:
            raise ValueError("Research reload differs from legacy input")
        for reference in references:
            await content.verify(reference)


async def _ledger(item: LegacyInput, database: Database, root: Path) -> None:
    assert item.cwd is not None
    workspace = workspace_id(item.cwd)
    tables = {
        "runs": s.tool_runs,
        "steps": s.tool_steps,
        "operations": s.tool_operations,
        "attempts": s.tool_attempts,
        "api_attempts": s.api_attempts,
    }
    operation_ids = {
        row["operation_id"]: hashlib.sha256(
            json.dumps([workspace, row["session_id"], row["scope"], row["call_id"]]).encode()
        ).hexdigest()
        for row in item.payload["operations"]
    }
    interrupted = {
        row["operation_id"]: "failed" if row["effect"] == "read_only" else "uncertain"
        for row in item.payload["operations"]
        if row["status"] == "running"
    }
    interrupted_steps = {
        (row["run_id"], row["step_id"]): interrupted[row["operation_id"]]
        for row in item.payload["operations"]
        if row["operation_id"] in interrupted
    }
    async with database.transaction() as db:
        await db.execute(
            insert(s.workspaces)
            .values(workspace_id=workspace, canonical_path=item.cwd, updated_at=time.time())
            .on_conflict_do_nothing()
        )
        for name, table in tables.items():
            for original in item.payload[name]:
                row = {key: value for key, value in original.items() if key in table.c}
                if name in {"runs", "operations", "api_attempts"}:
                    row["workspace_id"] = workspace
                if name == "runs" and any(run == row["run_id"] for run, _ in interrupted_steps):
                    row["status"] = "blocked"
                if name == "steps" and (row["run_id"], row["step_id"]) in interrupted_steps:
                    row["status"] = interrupted_steps[row["run_id"], row["step_id"]]
                if (
                    name == "attempts"
                    and row["operation_id"] in interrupted
                    and row["status"] == "running"
                ):
                    row["status"] = interrupted[row["operation_id"]]
                if name == "operations":
                    row["resources"] = json.loads(row["resources"])
                    if row.get("result_ref"):
                        path = _safe_file(root, Path(row["result_ref"]))
                        body = await asyncio.to_thread(path.read_bytes)
                        if hashlib.sha256(body).hexdigest() != item.content_hashes[str(path)]:
                            raise ValueError("Receipt content changed during import")
                        row["result_ref"] = await persist_object(database, workspace, body)
                    if row["status"] == "running":
                        row.update(
                            status="failed" if row["effect"] == "read_only" else "uncertain",
                            error_code="legacy_owner_unverified",
                            reconciliation="required",
                            host_id="legacy",
                        )
                if name == "api_attempts":
                    record = json.loads(row["record"])
                    row.update(
                        record=record,
                        session_id=item.session,
                        status=record["status"],
                        updated=record.get("finished", record.get("started", 0)),
                    )
                if "run_id" in row:
                    row["run_id"] = f"{workspace}:{row['run_id']}"
                if row.get("predecessor"):
                    row["predecessor"] = f"{workspace}:{row['predecessor']}"
                if "operation_id" in row:
                    row["operation_id"] = operation_ids[row["operation_id"]]
                # Remote idempotency keys remain unchanged; only local identities gain scope.
                keys = [column.name for column in table.primary_key]
                where = [table.c[key] == row[key] for key in keys]
                previous = (await db.execute(select(table).where(*where))).mappings().one_or_none()
                if previous is not None:
                    if any(previous[key] != value for key, value in row.items()):
                        raise ValueError(f"Existing {name} record conflicts with legacy input")
                    continue
                await db.execute(insert(table).values(**row))
                reloaded = (await db.execute(select(table).where(*where))).mappings().one()
                if any(reloaded[key] != value for key, value in row.items()):
                    raise ValueError(f"Reload verification failed for {name}")


async def import_legacy(
    root: Path, workspaces: list[Path], database: Database | None = None
) -> dict[str, Any]:
    inputs, report = await asyncio.to_thread(discover, root, workspaces)
    if database is None:
        return report
    report["dry_run"] = False
    if report["errors"]:
        report["aborted_before_writes"] = True
        return report
    with bind_database(database):
        for item in inputs:
            try:
                assert item.cwd is not None and item.session is not None
                workspace = workspace_id(item.cwd)
                async with database.transaction() as db:
                    previous = (
                        (
                            await db.execute(
                                select(s.legacy_import_records)
                                .where(s.legacy_import_records.c.source_path == str(item.path))
                                .with_for_update()
                            )
                        )
                        .mappings()
                        .one_or_none()
                    )
                    if previous:
                        if previous["source_hash"] != item.fingerprint:
                            raise ValueError(
                                "Previously imported source changed; refusing overwrite"
                            )
                        report["counts"][item.kind]["skipped"] += 1
                        continue
                    # Unique insertion claims the source within the same transaction as its records.
                    claimed = await db.scalar(
                        insert(s.legacy_import_records)
                        .values(
                            source_path=str(item.path),
                            source_hash=item.fingerprint,
                            kind=item.kind,
                            workspace_id=workspace,
                            session_id=item.session,
                            report={"verified": True},
                            imported_at=time.time(),
                        )
                        .on_conflict_do_nothing()
                        .returning(s.legacy_import_records.c.source_path)
                    )
                    if claimed is None:
                        fingerprint = await db.scalar(
                            select(s.legacy_import_records.c.source_hash).where(
                                s.legacy_import_records.c.source_path == str(item.path)
                            )
                        )
                        if fingerprint != item.fingerprint:
                            raise ValueError("Source changed during concurrent import")
                        report["counts"][item.kind]["skipped"] += 1
                        continue
                    if item.kind == "research":
                        await _research(item, database)
                    elif item.kind in {"web", "cli", "subagent", "context_archive"}:
                        conversations = ConversationRecords(database, item.cwd)
                        existing = await conversations.load(item.session)
                        if existing is not None and existing != item.payload:
                            # A byte-equivalent latest/named CLI snapshot may lack derived count metadata.
                            from researchx.services.sessions.storage import (
                                _sanitize_snapshot_payload,
                            )

                            if _sanitize_snapshot_payload(existing) != _sanitize_snapshot_payload(
                                item.payload
                            ):
                                raise ValueError(
                                    "Existing conversation differs; refusing overwrite"
                                )
                        if existing is None:
                            await conversations.write(item.payload, channel=item.kind)
                        loaded = await conversations.load(item.session)
                        from researchx.services.sessions.storage import _sanitize_snapshot_payload

                        expected = _sanitize_snapshot_payload(item.payload)
                        expected.pop(
                            "research_progress", None
                        )  # A derived projection, never authority.
                        if item.kind == "subagent":
                            for message in expected["messages"]:
                                message.pop("reasoning_content", None)
                        if loaded != expected:
                            raise ValueError("Conversation reload verification failed")
                        if item.kind == "context_archive":
                            body = _safe_file(root, item.path).read_bytes()
                            if hashlib.sha256(body).hexdigest() != item.fingerprint:
                                raise ValueError("Context archive changed during import")
                            reference = await persist_object(database, workspace, body)
                            await db.execute(
                                insert(s.file_records).values(
                                    workspace_id=workspace,
                                    session_id=item.session,
                                    record_id=item.session,
                                    kind="context_archive",
                                    content_hash=reference.removeprefix("sha256:"),
                                    payload={
                                        "legacy_source": str(item.path),
                                        "parent_session_id": item.payload["parent_session_id"],
                                    },
                                )
                            )
                    elif item.kind == "tool_content":
                        await ensure_session(db, workspace, item.session, item.cwd)
                        body = await asyncio.to_thread(_safe_file(root, item.path).read_bytes)
                        if hashlib.sha256(body).hexdigest() != item.fingerprint:
                            raise ValueError("Tool content changed during import")
                        reference = await persist_object(database, workspace, body)
                        await db.execute(
                            insert(s.file_records).values(
                                workspace_id=workspace,
                                session_id=item.session,
                                record_id=item.payload["id"],
                                kind="tool_content",
                                content_hash=reference.removeprefix("sha256:"),
                                payload=item.payload,
                            )
                        )
                        loaded = await db.scalar(
                            select(s.file_records.c.payload).where(
                                scope(s.file_records, workspace, item.session),
                                s.file_records.c.record_id == item.payload["id"],
                            )
                        )
                        if loaded != item.payload:
                            raise ValueError("Tool content metadata reload verification failed")
                    elif item.kind == "ledger":
                        await _ledger(item, database, root)
                    elif item.kind == "files":
                        await ensure_session(db, workspace, item.session, item.cwd)
                        filename = item.payload.get("original") or item.payload["filename"]
                        path = _safe_file(root, item.path.parent / filename)
                        body = await asyncio.to_thread(path.read_bytes)
                        if hashlib.sha256(body).hexdigest() != item.content_hashes[str(path)]:
                            raise ValueError("Attachment content changed during import")
                        reference = await persist_object(database, workspace, body)
                        await db.execute(
                            insert(s.file_records).values(
                                workspace_id=workspace,
                                session_id=item.session,
                                record_id=item.payload["id"],
                                kind=item.path.parent.parent.name,
                                content_hash=reference.removeprefix("sha256:"),
                                payload=item.payload,
                            )
                        )
                        loaded = await db.scalar(
                            select(s.file_records.c.payload).where(
                                scope(s.file_records, workspace, item.session),
                                s.file_records.c.record_id == item.payload["id"],
                            )
                        )
                        if loaded != item.payload:
                            raise ValueError("File metadata reload verification failed")
                    elif item.kind == "dispatch":
                        await ensure_session(db, workspace, item.session, item.cwd)
                        batch = dict(item.payload)
                        if batch.get("status") == "running":
                            batch["status"] = "interrupted"
                            batch["recovered"] = True
                        await db.execute(
                            insert(s.dispatches).values(
                                workspace_id=workspace,
                                session_id=item.session,
                                dispatch_id=batch["dispatch_id"],
                                status=batch["status"],
                                owner="legacy:unverified",
                                payload=batch,
                            )
                        )
                        loaded = await db.scalar(
                            select(s.dispatches.c.payload).where(
                                scope(s.dispatches, workspace, item.session),
                                s.dispatches.c.dispatch_id == batch["dispatch_id"],
                            )
                        )
                        if loaded != batch:
                            raise ValueError("Dispatch reload verification failed")
                report["counts"][item.kind]["imported"] += 1
            except (ValueError, KeyError, OSError, SQLAlchemyError) as exc:
                report["counts"][item.kind]["failed"] += 1
                report["errors"].append(
                    {"path": str(item.path), "kind": item.kind, "error": _diagnostic(exc)}
                )
    return report


async def main() -> int:
    parser = argparse.ArgumentParser(description="一次性导入旧业务存储；源文件只读")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, action="append", default=[])
    parser.add_argument(
        "--apply", action="store_true", help="写入已迁移的 PostgreSQL；默认只做 dry-run"
    )
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if args.apply:
        async with Database() as database:
            report = await import_legacy(args.source, args.workspace, database)
    else:
        report = await import_legacy(args.source, args.workspace)
    atomic_write_text(
        args.report, json.dumps(report, ensure_ascii=False, indent=2) + "\n", mode=0o600
    )
    print(
        json.dumps(
            {"dry_run": report["dry_run"], "counts": report["counts"], "report": str(args.report)},
            ensure_ascii=False,
        )
    )
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
