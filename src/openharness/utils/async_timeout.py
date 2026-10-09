"""One timeout contract on Python 3.10 and 3.11, including cancellation."""

from __future__ import annotations

import sys
from typing import AsyncContextManager

if sys.version_info >= (3, 11):
    import asyncio

    def timeout(seconds: float | None) -> AsyncContextManager[object]:
        return asyncio.timeout(seconds)
else:
    import async_timeout

    def timeout(seconds: float | None) -> AsyncContextManager[object]:
        return async_timeout.timeout(seconds)
