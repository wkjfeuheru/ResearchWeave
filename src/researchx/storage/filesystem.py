"""Atomic file-write helpers for persistent state.

Reserved files such as credentials, settings, workspace content and explicit
exports use atomic writes so a crash never exposes a truncated payload.
Structured sessions, research state and execution receipts are authoritative
in PostgreSQL; these helpers do not provide a JSON storage fallback.

The pattern implemented here is the standard temp-file-plus-rename dance:

1. Create a same-directory temp file (so the final :func:`os.replace` is a
   rename on the same filesystem, never a cross-filesystem copy).
2. Write the payload, ``flush`` and ``fsync``.
3. Apply the target POSIX mode while the file is still private.
4. :func:`os.replace` atomically swaps the temp file into place. On POSIX
   the kernel guarantees that any concurrent reader sees either the old
   inode or the new one, never a half-written one. Since Python 3.3
   :func:`os.replace` provides the same guarantee on Windows.

For read-modify-write sequences on shared files (credentials, settings, cron
registry), pair atomic writes with :func:`exclusive_file_lock` from
the local file lock so two concurrent ``rx`` processes cannot
clobber each other's updates.
"""

from __future__ import annotations

import contextlib
import os
import stat
import tempfile
import logging
from pathlib import Path

__all__ = ["atomic_write_bytes", "atomic_write_text"]


def atomic_write_bytes(
    path: str | os.PathLike[str], data: bytes, *, mode: int | None = None
) -> None:
    """Write ``data`` to ``path`` atomically.

    When ``mode`` is given, the final file is created with that POSIX mode
    even if it did not previously exist. When ``mode`` is ``None``, the
    existing file's mode is preserved; for new files the current umask
    determines the mode, matching the historical behaviour of
    :meth:`pathlib.Path.write_text`.
    """
    dst = Path(path)
    dst.parent.mkdir(parents=True, exist_ok=True)
    target_mode = _resolve_target_mode(dst, mode)

    fd, tmp_name = tempfile.mkstemp(prefix=f".{dst.name}.", suffix=".tmp", dir=str(dst.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as tmp_file:
            tmp_file.write(data)
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        _apply_mode(tmp_path, target_mode, strict=mode is not None)
        os.replace(tmp_path, dst)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise


def atomic_write_text(
    path: str | os.PathLike[str],
    data: str,
    *,
    encoding: str = "utf-8",
    mode: int | None = None,
) -> None:
    """Text variant of :func:`atomic_write_bytes`."""
    atomic_write_bytes(path, data.encode(encoding), mode=mode)


def _resolve_target_mode(path: Path, explicit_mode: int | None) -> int:
    if explicit_mode is not None:
        return explicit_mode
    try:
        st = path.stat()
    except FileNotFoundError:
        current_umask = os.umask(0)
        os.umask(current_umask)
        return 0o666 & ~current_umask
    return stat.S_IMODE(st.st_mode)


def _apply_mode(path: Path, target_mode: int, *, strict: bool = False) -> None:
    try:
        os.chmod(path, target_mode)
        if strict and os.name == "posix" and stat.S_IMODE(path.stat().st_mode) != target_mode:
            raise PermissionError("Filesystem did not enforce the requested privacy mode")
    except OSError:
        if strict and os.name == "posix":
            raise
        if strict:
            logging.getLogger(__name__).warning(
                "POSIX privacy modes are unavailable on this filesystem"
            )


def private_directory(path: Path) -> Path:
    """Create/harden a reserved storage directory, never chmod existing ancestors."""
    path = Path(path)
    missing = []
    parent = path
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise ValueError("Private storage directory must not be a symlink")
    if os.name == "posix":
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            # Reads revalidate privacy without dirtying directory metadata on
            # every access; only an actual permission repair needs chmod.
            if stat.S_IMODE(os.fstat(fd).st_mode) != 0o700:
                os.fchmod(fd, 0o700)
                if stat.S_IMODE(os.fstat(fd).st_mode) != 0o700:
                    raise PermissionError("Private directory mode cannot be enforced")
        finally:
            os.close(fd)
    else:
        _apply_mode(path, 0o700, strict=True)
    return path


def private_file(path: Path) -> None:
    """Harden an existing sensitive regular file without following symlinks."""
    if not path.exists() and not path.is_symlink():
        return
    if os.name != "posix":
        _apply_mode(path, 0o600, strict=True)
        return
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return  # A concurrent removal may win after the existence check.
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("Private storage file must be regular")
        os.fchmod(fd, 0o600)
        if stat.S_IMODE(os.fstat(fd).st_mode) != 0o600:
            raise PermissionError("Private file mode cannot be enforced")
    finally:
        os.close(fd)
