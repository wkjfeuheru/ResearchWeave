"""Small spawn target: avoid importing API SDKs just to access PostgreSQL."""

import os
import asyncio
from researchx.storage.database import Database, bind_database
from researchx.services.execution.operations import OperationStore


def open_store(path, queue):
    asyncio.run(_open_store(path, queue))


async def _open_store(path, queue):
    try:
        async with Database() as database:
            with bind_database(database):
                store = OperationStore(path)
                (await store.prepare(
                    session="s",
                    scope="w",
                    run=str(os.getpid()),
                    call=str(os.getpid()),
                    tool="tool",
                    version="1",
                    digest="digest",
                    effect="read_only",
                    resources={"read": ["*"], "write": []},
                ))
        queue.put("ok")
    except Exception as exc:
        queue.put(type(exc).__name__ + ":" + str(exc))
