"""Small spawn target: avoid importing API SDKs just to initialize SQLite."""

import os
from openharness.services.operations import OperationStore


def open_store(path, queue):
    try:
        store = OperationStore(path)
        store.prepare(
            session="s",
            scope="w",
            run=str(os.getpid()),
            call=str(os.getpid()),
            tool="tool",
            version="1",
            digest="digest",
            effect="read_only",
            resources={"read": ["*"], "write": []},
        )
        queue.put("ok")
    except Exception as exc:
        queue.put(type(exc).__name__ + ":" + str(exc))
