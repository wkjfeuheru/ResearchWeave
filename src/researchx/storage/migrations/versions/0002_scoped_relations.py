"""Scoped entity references and recovery/retention indexes."""

import sqlalchemy as sa
from alembic import op

revision = "0002_scoped_relations"
down_revision = "0001_postgresql"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table, column, target in (
        ("research_plans", "context_id", "research_contexts"),
        ("research_sources", "execution_id", "research_executions"),
        ("research_evidence", "source_id", "research_sources"),
        ("research_artifacts", "execution_id", "research_executions"),
    ):
        op.add_column(table, sa.Column(column, sa.Text()))
        op.execute(sa.text(f"UPDATE {table} SET {column} = payload ->> '{column}'"))
        op.create_foreign_key(
            f"fk_{table}_{column}",
            table,
            target,
            ["workspace_id", "session_id", column],
            ["workspace_id", "session_id", "record_id"],
            deferrable=True,
            initially="DEFERRED",
        )
    for name, table, columns in (
        ("ix_evidence_source", "research_evidence", ["workspace_id", "session_id", "source_id"]),
        (
            "ix_plans_revision",
            "research_plans",
            ["workspace_id", "session_id", "project_id", "revision"],
        ),
        (
            "ix_artifacts_task",
            "research_artifacts",
            ["workspace_id", "session_id", "task_id", "status"],
        ),
        ("ix_projects_status", "research_projects", ["workspace_id", "status"]),
        ("ix_research_workspace_updated", "research_sessions", ["workspace_id", "updated_at"]),
        ("ix_operations_resources", "tool_operations", ["workspace_id", "scope", "status"]),
        ("ix_api_retention", "api_attempts", ["workspace_id", "status", "updated"]),
        ("ix_attempts_operation", "tool_attempts", ["operation_id", "status", "updated"]),
    ):
        op.create_index(name, table, columns)
    for table in ("tool_runs", "tool_operations", "api_attempts"):
        op.create_foreign_key(
            f"fk_{table}_workspace", table, "workspaces", ["workspace_id"], ["workspace_id"]
        )
    op.create_unique_constraint("uq_tool_run_workspace", "tool_runs", ["run_id", "workspace_id"])
    op.create_foreign_key(
        "fk_operation_run_workspace",
        "tool_operations",
        "tool_runs",
        ["run_id", "workspace_id"],
        ["run_id", "workspace_id"],
    )
    op.create_unique_constraint("uq_legacy_source_path", "legacy_import_records", ["source_path"])


def downgrade() -> None:
    raise RuntimeError(
        "Restore a verified database backup instead of weakening integrity constraints"
    )
