"""Durable candidate dispatch audit and nonblocking process-lifetime ownership."""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from typing_extensions import TypedDict
from pydantic import TypeAdapter, ValidationError
from openharness.utils.fs import atomic_write_text, private_directory
from openharness.research.errors import ResearchError


class AssignmentRecord(TypedDict):
    task_id: str
    instruction: str
    context: str | None


class DispatchAudit(TypedDict, total=False):
    dispatch_id: str
    project_id: str
    parent_task_id: str
    parent_execution_id: str | None
    task_revision: int
    runtime_id: str | None
    schema_version: int
    retry_of: str | None
    baseline: list[int]
    status: str
    tasks: list[AssignmentRecord]
    results: list[dict[str, object]]
    recovered: bool


def dispatch_directory(store_directory: Path, dispatch_id: str) -> Path:
    if not re.fullmatch(r"dispatch_[a-f0-9]{32}", dispatch_id):
        raise ResearchError("Invalid dispatch ID")
    directory = store_directory / "dispatches" / dispatch_id
    if directory.is_symlink() or directory.resolve() != directory.absolute():
        raise ResearchError("Invalid dispatch audit directory")
    return directory


@contextmanager
def lifecycle_lock(directory: Path) -> Iterator[bool]:
    """False means an owner is alive. The kernel releases this lock on SIGKILL."""
    private_directory(directory)
    with (directory / ".lifecycle.lock").open("a+b") as stream:
        acquired = False
        if __import__("os").name == "nt":
            import msvcrt

            stream.write(b"\0")
            stream.flush()
            stream.seek(0)
            try:
                getattr(msvcrt, "locking")(stream.fileno(), getattr(msvcrt, "LK_NBLCK"), 1)
                acquired = True
            except OSError:
                pass
        else:
            import fcntl

            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                pass
        try:
            yield acquired
        finally:
            if acquired:
                if __import__("os").name == "nt":
                    stream.seek(0)
                    getattr(msvcrt, "locking")(stream.fileno(), getattr(msvcrt, "LK_UNLCK"), 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def read_batch(directory: Path) -> DispatchAudit:
    try:
        batch = TypeAdapter(DispatchAudit).validate_json(
            (directory / "batch.json").read_text(encoding="utf-8"), strict=True
        )
    except (ValueError, ValidationError) as exc:
        raise ResearchError("Invalid dispatch audit; retain it for inspection") from exc
    if batch.get("schema_version", 1) not in {1, 2}:
        raise ResearchError("Unsupported dispatch audit schema; no recovery was applied")
    if batch.get("dispatch_id") != directory.name:
        raise ResearchError("Dispatch audit identity mismatch")
    return batch


def recover_dispatches(
    store_directory: Path, project_id: str, workspace: Path | None = None
) -> bool:
    """Merge individual durable results. Return True when any dispatch is alive."""
    active = False
    for candidate in sorted((store_directory / "dispatches").glob("dispatch_*")):
        directory = dispatch_directory(store_directory, candidate.name)
        batch_file = directory / "batch.json"
        with lifecycle_lock(directory) as acquired:
            if not batch_file.exists():
                active |= not acquired  # Owner may still be creating its first checkpoint.
                continue
            batch = read_batch(directory)
            if batch.get("project_id") != project_id:
                continue
            if not acquired:
                active = True
                continue
            if batch.get("status") != "running":
                continue
            results = []
            for task in batch.get("tasks", []):
                task_id = task["task_id"]
                if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", task_id):
                    raise ResearchError("Invalid persisted subagent task ID")
                result_file = directory / task_id / "result.json"
                if result_file.exists():
                    result = json.loads(result_file.read_text(encoding="utf-8"))
                    if result.get("task_id") != task_id:
                        raise ResearchError("Subagent audit identity mismatch")
                else:
                    result = {
                        "task_id": task_id,
                        "status": "interrupted",
                        "summary": "",
                        "evidence_refs": [],
                        "output_paths": [],
                        "errors": ["Dispatch owner exited before durable completion"],
                    }
                if workspace and result.get("status") == "interrupted":
                    from openharness.research.runtime import ResearchAgentRuntime

                    output = workspace / "subagents" / directory.name / task_id
                    paths = []
                    for path in sorted(output.rglob("*")):
                        try:
                            checked = ResearchAgentRuntime._check_path(workspace, path)
                            if checked.is_file():
                                paths.append(str(checked.relative_to(workspace)))
                        except (ResearchError, OSError):
                            continue
                        if len(paths) >= 50:
                            break
                    result["output_paths"] = paths
                results.append(result)
            # One atomic checkpoint, no per-child replay or authoritative mutations.
            batch.update({"status": "interrupted", "results": results, "recovered": True})
            atomic_write_text(batch_file, json.dumps(batch, ensure_ascii=False), mode=0o600)
    return active


def dispatch_summaries(store_directory: Path, project_id: str) -> list[dict[str, object]]:
    summaries = []
    for path in sorted((store_directory / "dispatches").glob("*/batch.json")):
        batch = read_batch(dispatch_directory(store_directory, path.parent.name))
        if batch.get("project_id") == project_id:
            summaries.append(
                {
                    key: batch.get(key)
                    for key in (
                        "dispatch_id",
                        "schema_version",
                        "parent_execution_id",
                        "parent_task_id",
                        "task_revision",
                        "baseline",
                        "status",
                        "retry_of",
                        "results",
                    )
                }
                | {"candidate_authority": "unreviewed", "requires_main_review": True}
            )
    return summaries
