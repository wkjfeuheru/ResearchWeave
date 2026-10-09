"""Filename normalization for workspace resources."""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path


def safe_filename(value: object, *, max_length: int = 128) -> str:
    """Return a conservative filename-safe representation of ``value``.

    Path separators, control characters, shell metacharacters, and whitespace
    collapse to underscores. The result is a single basename, not a path.
    """

    if value is None:
        return ""
    name = Path(str(value)).name
    name = unicodedata.normalize("NFKC", name)
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._-")
    if not name or name in {".", ".."}:
        return ""
    return name[:max_length]
