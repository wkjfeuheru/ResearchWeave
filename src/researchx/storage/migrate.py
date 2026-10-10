"""Explicit migration entry point: python -m researchx.storage.migrate."""

from __future__ import annotations

import asyncio
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Connection
from sqlalchemy import text

from researchx.storage.database import Database


async def upgrade(database: Database, revision: str = "head") -> None:
    def apply(connection: Connection) -> None:
        config = Config()
        config.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
        config.attributes["connection"] = connection
        command.upgrade(config, revision)

    async with database.engine.begin() as connection:
        # Serialize explicit deploy migrations across processes, without runtime DDL.
        await connection.execute(text("SELECT pg_advisory_xact_lock(731929142510)"))
        await connection.run_sync(apply)


async def main() -> None:
    database = Database()
    try:
        await upgrade(database)
        await database.check()
    finally:
        await database.close()


if __name__ == "__main__":
    asyncio.run(main())
