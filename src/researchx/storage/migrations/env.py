"""Alembic runs on the explicit async migration connection, never a request."""

from alembic import context
from researchx.storage.schema import metadata

connection = context.config.attributes["connection"]
context.configure(connection=connection, target_metadata=metadata)
with context.begin_transaction():
    context.run_migrations()
