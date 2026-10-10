"""Explicit fault injection into PostgreSQL; never used by production code."""

from sqlalchemy import update
from researchx.storage import schema as s
from researchx.storage.research_records import scope


async def raw_state(store):
    async with store.transaction() as db:
        return (await store.records.load(db)).model_dump(mode="json")


async def corrupt_state(store, data):
    """Change existing rows without domain validation, emulating damaged persisted records."""
    async with store.transaction() as db:
        await db.execute(update(s.research_sessions).where(
            scope(s.research_sessions, store.workspace_id, store.session_id)
        ).values(revision=data["revision"], current_context_id=data["current_context_id"],
                 research_state=data["research_state"]))
        for field, table in s.entities.items():
            items = data[field]
            if field == "task_context":
                items = {item["id"]: item for item in items}
            elif field == "project":
                items = {items["id"]: items} if items else {}
            for identity, original in items.items():
                payload = dict(original)
                if field == "plans":
                    for task in payload.pop("tasks"):
                        await db.execute(update(s.research_tasks).where(
                            scope(s.research_tasks, store.workspace_id, store.session_id),
                            s.research_tasks.c.plan_id == identity,
                            s.research_tasks.c.record_id == task["id"],
                        ).values(payload=task, status=task["status"], revision=task.get("task_revision", 1)))
                fields = {key: payload.get(key) for key in (
                    "status", "revision", "project_id", "plan_id", "task_id", "content_hash",
                    "source_id", "execution_id", "context_id",
                ) if key in table.c}
                await db.execute(update(table).where(
                    scope(table, store.workspace_id, store.session_id), table.c.record_id == str(identity),
                ).values(payload=payload, **fields))


async def source_path(store, source):
    reference = await store._content_reference(source.content_hash)
    return store.content.root / reference.object_key


async def ledger_rows(store, table):
    from sqlalchemy import select
    async with store.database.transaction() as db:
        query = select(table)
        if "workspace_id" in table.c:
            query = query.where(table.c.workspace_id == store.workspace)
        else:
            query = query.join(s.tool_operations, table.c.operation_id == s.tool_operations.c.operation_id).where(s.tool_operations.c.workspace_id == store.workspace)
        return list((await db.execute(query)).mappings().all())
