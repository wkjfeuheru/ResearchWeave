"""Transactional local execution receipts, independent of chat snapshots."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import threading
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4
from typing import Any, Iterator
from researchx.storage.filesystem import private_directory, private_file

STATES = {
    "prepared",
    "running",
    "succeeded",
    "failed",
    "partial",
    "uncertain",
    "cancelled",
    "blocked",
}
EDGES = {
    "prepared": {"running", "blocked", "cancelled"},
    "running": {"succeeded", "failed", "partial", "uncertain", "cancelled", "blocked"},
    "failed": set(),
    "blocked": set(),
    "cancelled": set(),
    "succeeded": set(),
    "uncertain": {"succeeded", "failed"},
    "partial": {"succeeded", "failed"},
}


_SCHEMA_LOCK = threading.RLock()
_INITIALIZED: OrderedDict[tuple[int, str], tuple[int, int]] = OrderedDict()
_SCHEMA_VERSION = 2


def _after_fork() -> None:
    global _SCHEMA_LOCK
    _SCHEMA_LOCK = threading.RLock()
    _INITIALIZED.clear()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)


@contextmanager
def _schema_initialization_lock() -> Iterator[None]:
    if not _SCHEMA_LOCK.acquire(timeout=5.0):
        raise TimeoutError("Operation schema initialization is busy")
    try:
        yield
    finally:
        _SCHEMA_LOCK.release()


class OperationStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path).absolute()
        private_directory(self.path.parent)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.close(fd)
        self._private_files()
        self._initialize()

    def _initialize(self) -> None:
        key = (os.getpid(), str(self.path))
        with _schema_initialization_lock():
            st = self.path.stat()
            identity = (st.st_dev, st.st_ino)
            if _INITIALIZED.get(key) == identity and st.st_size > 0:
                _INITIALIZED.move_to_end(key)
                return
            with self.connect(timeout=5.0) as db:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("BEGIN IMMEDIATE")
                try:
                    version = db.execute("PRAGMA user_version").fetchone()[0]
                    if version > _SCHEMA_VERSION:
                        raise ValueError("Operation database has a newer unsupported schema")
                    if version < _SCHEMA_VERSION:
                        schema = """
                CREATE TABLE IF NOT EXISTS schema_version(version INTEGER PRIMARY KEY);
                INSERT OR IGNORE INTO schema_version VALUES(1);
                CREATE TABLE IF NOT EXISTS api_attempts(
                    attempt_id TEXT PRIMARY KEY, request_id TEXT NOT NULL, record TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runs(
                    run_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, scope TEXT NOT NULL,
                    status TEXT NOT NULL, predecessor TEXT, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS steps(
                    run_id TEXT NOT NULL, step_id TEXT NOT NULL, status TEXT NOT NULL,
                    updated REAL NOT NULL, PRIMARY KEY(run_id,step_id));
                CREATE TABLE IF NOT EXISTS operations(
                    operation_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, session_id TEXT NOT NULL,
                    scope TEXT NOT NULL, step_id TEXT NOT NULL, call_id TEXT NOT NULL,
                    tool TEXT NOT NULL, contract_version TEXT NOT NULL, input_digest TEXT NOT NULL,
                    effect TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    owner TEXT, pid INTEGER, lease_until REAL, created REAL NOT NULL, updated REAL NOT NULL,
                    error_code TEXT, result_ref TEXT, external_request_id TEXT, idempotency_key TEXT NOT NULL,
                    reconciliation TEXT, resources TEXT NOT NULL,
                    UNIQUE(session_id,scope,call_id));
                CREATE TABLE IF NOT EXISTS attempts(
                    attempt_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL, number INTEGER NOT NULL,
                    status TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
                    UNIQUE(operation_id,number));
            """
                        for statement in schema.split(";"):
                            if statement.strip():
                                db.execute(statement)
                        columns = {row[1] for row in db.execute("PRAGMA table_info(api_attempts)")}
                        for column, kind in (("status", "TEXT"), ("updated", "REAL")):
                            if column not in columns:
                                db.execute(f"ALTER TABLE api_attempts ADD COLUMN {column} {kind}")
                        for statement in (
                            "CREATE INDEX IF NOT EXISTS operation_scope_status ON operations(scope,status)",
                            "CREATE INDEX IF NOT EXISTS operation_session_status ON operations(session_id,status,scope)",
                            "CREATE INDEX IF NOT EXISTS operation_run_status ON operations(run_id,status)",
                            "CREATE INDEX IF NOT EXISTS attempt_operation_status ON attempts(operation_id,status)",
                            "CREATE INDEX IF NOT EXISTS api_status_updated ON api_attempts(status,updated)",
                        ):
                            db.execute(statement)
                        db.execute(
                            "INSERT OR IGNORE INTO schema_version VALUES(?)", (_SCHEMA_VERSION,)
                        )
                        db.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
                    db.commit()
                except BaseException:
                    db.rollback()
                    raise
            _INITIALIZED[key] = identity
            while len(_INITIALIZED) > 256:
                _INITIALIZED.popitem(last=False)

    def _private_files(self) -> None:
        for path in (self.path, Path(str(self.path) + "-wal"), Path(str(self.path) + "-shm")):
            private_file(path)

    @contextmanager
    def connect(self, *, timeout: float = 0.25) -> Iterator[sqlite3.Connection]:
        self._private_files()
        db = sqlite3.connect(self.path, timeout=timeout, isolation_level=None)
        self._private_files()
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except BaseException:
                db.rollback()
                raise
            else:
                db.commit()

    def prepare(
        self,
        *,
        session: str,
        scope: str,
        run: str,
        call: str,
        tool: str,
        version: str,
        digest: str,
        effect: str,
        resources: dict[str, list[str]],
    ) -> dict[str, Any]:
        identity = json.dumps([session, scope, call])
        operation = hashlib.sha256(identity.encode()).hexdigest()
        now = time.time()
        with self.transaction() as db:
            previous = db.execute(
                "SELECT * FROM operations WHERE operation_id=?", (operation,)
            ).fetchone()
            if previous is not None:
                if (
                    previous["input_digest"] != digest
                    or previous["tool"] != tool
                    or previous["contract_version"] != version
                ):
                    raise ValueError("Operation ID already bound to different input/tool/contract")
                return dict(previous)
            db.execute(
                "INSERT OR IGNORE INTO runs VALUES(?,?,?,?,?,?)",
                (run, session, scope, "running", None, now),
            )
            db.execute("INSERT OR IGNORE INTO steps VALUES(?,?,?,?)", (run, call, "prepared", now))
            db.execute(
                """INSERT OR IGNORE INTO operations
                (operation_id,run_id,session_id,scope,step_id,call_id,tool,contract_version,input_digest,
                 effect,status,created,updated,idempotency_key,resources)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    operation,
                    run,
                    session,
                    scope,
                    call,
                    call,
                    tool,
                    version,
                    digest,
                    effect,
                    "prepared",
                    now,
                    now,
                    operation,
                    json.dumps(resources),
                ),
            )
            row = dict(
                db.execute("SELECT * FROM operations WHERE operation_id=?", (operation,)).fetchone()
            )
            if (
                row["input_digest"] != digest
                or row["tool"] != tool
                or row["contract_version"] != version
            ):
                raise ValueError("Operation ID already bound to different input/tool/contract")
            return row

    def record_api_attempt(self, record: dict[str, Any]) -> None:
        with self.transaction() as db:
            existing = db.execute(
                "SELECT record,request_id FROM api_attempts WHERE attempt_id=?",
                (record["attempt_id"],),
            ).fetchone()
            if existing and existing["request_id"] != record["request_id"]:
                raise ValueError("API attempt ID is bound to another request")
            if existing and json.loads(existing["record"])["status"] != "running":
                if json.loads(existing["record"]) == record:
                    return
                raise ValueError("API attempt already settled")
            db.execute(
                "INSERT INTO api_attempts(attempt_id,request_id,record,status,updated) VALUES(?,?,?,?,?) "
                "ON CONFLICT(attempt_id) DO UPDATE SET record=excluded.record,status=excluded.status,updated=excluded.updated",
                (
                    record["attempt_id"],
                    record["request_id"],
                    json.dumps(record),
                    record["status"],
                    record.get("finished", record.get("started")),
                ),
            )

    def prune_audit(self, *, before: float, limit: int = 1000) -> dict[str, int]:
        """Bounded host maintenance; preserve receipts, artifacts and unresolved/unknown usage."""
        if not 1 <= limit <= 10000:
            raise ValueError("Audit prune limit must be 1..10000")
        with self.transaction() as db:
            api = [
                row[0]
                for row in db.execute(
                    "SELECT attempt_id FROM api_attempts WHERE status IN ('succeeded','failed','cancelled') "
                    "AND updated<? AND CASE WHEN json_valid(record) THEN json_extract(record,'$.usage_status') END='reported' "
                    "ORDER BY updated LIMIT ?",
                    (before, limit),
                )
            ]
            db.executemany(
                "DELETE FROM api_attempts WHERE attempt_id=?", [(value,) for value in api]
            )
            remaining = limit - len(api)
            attempts = [
                row[0]
                for row in db.execute(
                    "SELECT a.attempt_id FROM attempts a JOIN operations o ON o.operation_id=a.operation_id "
                    "WHERE o.status IN ('succeeded','failed','cancelled') AND o.updated<? AND a.updated<? "
                    "AND a.status!='running' ORDER BY a.updated LIMIT ?",
                    (before, before, remaining),
                )
            ]
            db.executemany(
                "DELETE FROM attempts WHERE attempt_id=?", [(value,) for value in attempts]
            )
        return {"api_attempts": len(api), "tool_attempts": len(attempts)}

    def get(self, operation: str, *, session: str, scope: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM operations WHERE operation_id=? AND session_id=? AND scope=?",
                (operation, session, scope),
            ).fetchone()
            if row is None:
                raise ValueError("Unknown operation in this session/workspace")
            return dict(row)

    def unresolved_conflicts(self, operation: str) -> list[str]:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM operations WHERE operation_id=?", (operation,)
            ).fetchone()
            requested = json.loads(row["resources"])
            return [
                other["operation_id"]
                for other in db.execute(
                    "SELECT operation_id,resources FROM operations WHERE scope=? AND status IN ('uncertain','partial')",
                    (row["scope"],),
                )
                if _conflicts(requested, json.loads(other["resources"]))
            ]

    def claim(self, operation: str, owner: str) -> str | None:
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM operations WHERE operation_id=?", (operation,)
            ).fetchone()
            if row is None or row["status"] != "prepared":
                return None
            requested = json.loads(row["resources"])
            running = db.execute(
                "SELECT resources FROM operations WHERE scope=? AND status IN ('running','uncertain','partial')",
                (row["scope"],),
            )
            for other in running:
                if _conflicts(requested, json.loads(other["resources"])):
                    return None
            now = time.time()
            number = row["attempts"] + 1
            attempt = uuid4().hex
            db.execute(
                "UPDATE steps SET status='running',updated=? WHERE run_id=? AND step_id=?",
                (now, row["run_id"], row["step_id"]),
            )
            db.execute(
                "UPDATE runs SET status='running',updated=? WHERE run_id=?", (now, row["run_id"])
            )
            db.execute(
                "UPDATE operations SET status='running',owner=?,pid=?,lease_until=?,attempts=?,updated=? WHERE operation_id=?",
                (owner, os.getpid(), now + 3600, number, now, operation),
            )
            db.execute(
                "INSERT INTO attempts VALUES(?,?,?,?,?,?)",
                (attempt, operation, number, "running", now, now),
            )
            return attempt

    def next_attempt(self, operation: str, *, owner: str) -> str:
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM operations WHERE operation_id=?", (operation,)
            ).fetchone()
            if row is None or row["owner"] != owner or row["status"] != "running":
                raise ValueError("Only the active owner may retry")
            now = time.time()
            db.execute(
                "UPDATE attempts SET status='failed',updated=? WHERE operation_id=? AND status='running'",
                (now, operation),
            )
            number = row["attempts"] + 1
            attempt = uuid4().hex
            db.execute(
                "INSERT INTO attempts VALUES(?,?,?,?,?,?)",
                (attempt, operation, number, "running", now, now),
            )
            db.execute(
                "UPDATE operations SET attempts=?,updated=? WHERE operation_id=?",
                (number, now, operation),
            )
            return attempt

    def settle(
        self,
        operation: str,
        status: str,
        *,
        owner: str | None = None,
        error_code: str | None = None,
        result_ref: str | None = None,
        external_request_id: str | None = None,
        evidence: str | None = None,
    ) -> None:
        if status not in STATES:
            raise ValueError("Invalid operation status")
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM operations WHERE operation_id=?", (operation,)
            ).fetchone()
            if row is None:
                raise ValueError("Unknown operation")
            if row["status"] == status:
                return
            if status not in EDGES[row["status"]]:
                raise ValueError(f"Illegal operation transition {row['status']} -> {status}")
            if row["status"] in {"uncertain", "partial"} and not evidence:
                raise ValueError("Reconciliation evidence is required")
            if row["owner"] and owner != row["owner"] and not evidence:
                raise ValueError("Operation is owned by another executor")
            now = time.time()
            db.execute(
                """UPDATE operations SET status=?,error_code=?,result_ref=COALESCE(?,result_ref),
                       external_request_id=COALESCE(?,external_request_id),reconciliation=?,updated=?,lease_until=NULL
                       WHERE operation_id=?""",
                (status, error_code, result_ref, external_request_id, evidence, now, operation),
            )
            db.execute(
                "UPDATE attempts SET status=?,updated=? WHERE operation_id=? AND status='running'",
                (status, now, operation),
            )
            db.execute(
                "UPDATE steps SET status=?,updated=? WHERE run_id=? AND step_id=?",
                (status, now, row["run_id"], row["step_id"]),
            )
            pending = db.execute(
                "SELECT 1 FROM operations WHERE run_id=? AND status IN ('running','prepared')",
                (row["run_id"],),
            ).fetchone()
            if not pending:
                states = {
                    item[0]
                    for item in db.execute(
                        "SELECT status FROM operations WHERE run_id=?", (row["run_id"],)
                    )
                }
                run_state = (
                    "blocked"
                    if states & {"uncertain", "partial", "blocked"}
                    else "failed"
                    if "failed" in states
                    else "cancelled"
                    if "cancelled" in states
                    else "succeeded"
                )
                db.execute(
                    "UPDATE runs SET status=?,updated=? WHERE run_id=?",
                    (run_state, now, row["run_id"]),
                )

    def retry_failed(self, operation: str, contract: Any, *, verified_no_effect: str) -> None:
        """Host recovery only, after explicit evidence; never called by continue_pending."""
        if not verified_no_effect or contract.retry_mode == "never":
            raise ValueError("Recovery requires evidence and a retryable contract")
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM operations WHERE operation_id=?", (operation,)
            ).fetchone()
            if row is None or row["status"] != "failed" or row["attempts"] >= contract.max_attempts:
                raise ValueError("Operation is not eligible for another attempt")
            if row["tool"] != contract.name or row["contract_version"] != contract.version:
                raise ValueError("Recovery contract mismatch")
            now = time.time()
            db.execute(
                "UPDATE operations SET status='prepared',owner=NULL,pid=NULL,reconciliation=?,updated=? WHERE operation_id=?",
                (verified_no_effect, now, operation),
            )
            db.execute(
                "UPDATE steps SET status='prepared',updated=? WHERE run_id=? AND step_id=?",
                (now, row["run_id"], row["step_id"]),
            )

    def recover(self, *, session: str, scope: str) -> list[dict[str, Any]]:
        """Only dead owners are reclaimed. Expired leases never steal from a live process."""
        with self.transaction() as db:
            rows = db.execute(
                "SELECT * FROM operations WHERE session_id=? AND (scope=? OR substr(scope,1,length(?))=?) AND status='running'",
                (session, scope, scope + ":child:", scope + ":child:"),
            ).fetchall()
            for row in rows:
                if _alive(row["pid"]):
                    continue
                state = "failed" if row["effect"] == "read_only" else "uncertain"
                db.execute(
                    "UPDATE operations SET status=?,error_code=?,reconciliation=?,updated=? WHERE operation_id=?",
                    (
                        state,
                        "owner_lost",
                        "required" if state == "uncertain" else "no_write_effect",
                        time.time(),
                        row["operation_id"],
                    ),
                )
                db.execute(
                    "UPDATE attempts SET status=?,updated=? WHERE operation_id=? AND status='running'",
                    (state, time.time(), row["operation_id"]),
                )
                db.execute(
                    "UPDATE steps SET status=?,updated=? WHERE run_id=? AND step_id=?",
                    (state, time.time(), row["run_id"], row["step_id"]),
                )
                db.execute(
                    "UPDATE runs SET status='blocked',updated=? WHERE run_id=?",
                    (time.time(), row["run_id"]),
                )
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM operations WHERE session_id=? AND scope=? AND status IN ('uncertain','partial')",
                    (session, scope),
                )
            ]


def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _conflicts(a: dict[str, list[str]], b: dict[str, list[str]]) -> bool:
    def intersects(xs: list[str], ys: list[str]) -> bool:
        return any(
            x == "*"
            or y == "*"
            or x == y
            or x.startswith(y.rstrip("/") + "/")
            or y.startswith(x.rstrip("/") + "/")
            for x in xs
            for y in ys
        )

    return intersects(a["write"], b["read"] + b["write"]) or intersects(
        b["write"], a["read"] + a["write"]
    )
