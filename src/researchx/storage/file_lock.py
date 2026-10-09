"""Cross-platform exclusive file-lock helpers.

Used to serialise read-modify-write sequences on shared JSON registries
(credentials, settings, research state). Pair with
:func:`researchx.storage.filesystem.atomic_write_text` to make each critical section
both race-free and crash-safe.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import os
from researchx.storage.filesystem import private_file
from typing import Iterator

from researchx.platforms import PlatformName, get_platform


class SwarmLockError(RuntimeError):
    """Base error for file-lock failures."""


class SwarmLockUnavailableError(SwarmLockError):
    """Raised when file locking is unavailable on the current platform."""


@contextmanager
def exclusive_file_lock(
    lock_path: Path,
    *,
    platform_name: PlatformName | None = None,
) -> Iterator[None]:
    """Acquire an exclusive file lock for the duration of the context."""
    resolved_platform = platform_name or get_platform()
    if resolved_platform == "windows":
        with _exclusive_windows_lock(lock_path):
            yield
        return
    if resolved_platform in {"macos", "linux", "wsl"}:
        with _exclusive_posix_lock(lock_path):
            yield
        return
    raise SwarmLockUnavailableError(
        f"file locking is not supported on platform {resolved_platform!r}"
    )


@contextmanager
def _exclusive_posix_lock(lock_path: Path) -> Iterator[None]:
    try:
        import fcntl
    except ImportError as exc:
        raise SwarmLockUnavailableError(f"fcntl not available: {exc}") from exc

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
    os.close(fd)
    private_file(lock_path)
    with lock_path.open("a+b") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


@contextmanager
def _exclusive_windows_lock(lock_path: Path) -> Iterator[None]:
    try:
        import msvcrt
    except ImportError as exc:
        raise SwarmLockUnavailableError(f"msvcrt not available: {exc}") from exc

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock_file:
        # msvcrt.locking requires a byte range to exist and the file be open
        # in binary mode. Lock the first byte for the lifetime of the
        # critical section.
        lock_file.seek(0)
        if lock_path.stat().st_size == 0:
            lock_file.write(b"\0")
            lock_file.flush()
        lock_file.seek(0)
        getattr(msvcrt, "locking")(lock_file.fileno(), getattr(msvcrt, "LK_LOCK"), 1)
        try:
            yield
        finally:
            lock_file.seek(0)
            getattr(msvcrt, "locking")(lock_file.fileno(), getattr(msvcrt, "LK_UNLCK"), 1)
