"""Explicit operator reconciliation of PostgreSQL operation receipts."""

from __future__ import annotations

import asyncio
import json
from enum import Enum
from pathlib import Path

import typer
from sqlalchemy.exc import SQLAlchemyError

from researchx.hooks.safety import redact
from researchx.services.execution.operations import OperationStore
from researchx.storage.database import database_lifespan

app = typer.Typer(name="operations", help="检查未决操作并由用户核验副作用；不会自动重放")


class Outcome(str, Enum):
    no_effect = "no_effect"
    succeeded = "succeeded"


@app.command("list")
def list_operations(
    cwd: Path = typer.Option(Path("."), help="原始工作区根目录"),
    session: str | None = typer.Option(None, help="可选会话 ID"),
) -> None:
    async def run() -> None:
        async with database_lifespan():
            rows = await OperationStore(cwd).list_unresolved(session=session)
            fields = (
                "operation_id",
                "session_id",
                "scope",
                "tool",
                "status",
                "attempts",
                "resources",
                "error_code",
                "reconciliation",
                "result_ref",
            )
            typer.echo(
                redact(
                    json.dumps(
                        [{key: row[key] for key in fields} for row in rows],
                        ensure_ascii=False,
                        indent=2,
                    )
                )
            )

    try:
        asyncio.run(run())
    except (ValueError, OSError, SQLAlchemyError) as exc:
        typer.echo(redact(f"操作查询失败：{exc}"), err=True)
        raise typer.Exit(1) from None


@app.command("resolve")
def resolve_operation(
    operation: str = typer.Argument(help="待核验的 operation ID"),
    session: str = typer.Option(..., help="收据所属会话 ID"),
    outcome: Outcome = typer.Option(..., help="用户已核验的结果；不得猜测"),
    evidence: str = typer.Option(..., help="核验依据、外部记录或补偿结果；不要包含密钥"),
    cwd: Path = typer.Option(Path("."), help="原始工作区根目录"),
    result_ref: str | None = typer.Option(None, help="succeeded 必须提供匹配的成功收据内容引用"),
) -> None:
    async def run() -> None:
        async with database_lifespan():
            await OperationStore(cwd).reconcile(
                operation,
                session=session,
                outcome=outcome.value,
                evidence=redact(evidence),
                result_ref=result_ref,
            )
        typer.echo("核验结果已持久化；未执行任何工具重试。")

    try:
        asyncio.run(run())
    except (ValueError, OSError, SQLAlchemyError) as exc:
        typer.echo(redact(f"操作核验失败：{exc}"), err=True)
        raise typer.Exit(1) from None
