"""Explicit parent/child conversation relationships, without child authority over research."""

from alembic import op
import sqlalchemy as sa

revision = "0003_child_sessions"
down_revision = "0002_scoped_relations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("research_sessions", sa.Column("parent_session_id", sa.String(128)))
    op.create_foreign_key(
        "fk_session_parent",
        "research_sessions",
        "research_sessions",
        ["workspace_id", "parent_session_id"],
        ["workspace_id", "session_id"],
        ondelete="CASCADE",
    )
    op.create_check_constraint(
        "ck_session_not_own_parent", "research_sessions", "parent_session_id <> session_id"
    )
    op.create_index("ix_session_parent", "research_sessions", ["workspace_id", "parent_session_id"])


def downgrade() -> None:
    raise RuntimeError("Restore a verified backup; do not discard conversation relationships")
