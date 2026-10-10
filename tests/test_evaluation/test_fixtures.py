"""Fixed tools execute calculations while refusing unregistered sources and gold."""

import json
import shutil

import pytest

from researchx.evaluation.dataset import DEFAULT_DATASET, load_cases
from researchx.evaluation.fixtures import FrozenExternalTool, RestrictedPythonTool, FaultTool
from researchx.evaluation.models import Fault
from researchx.state.store import ResearchStore
from researchx.tools.base import ToolExecutionContext
from researchx.tools.bash_tool import BashTool, BashToolInput
from researchx.tools.web_fetch_tool import WebFetchTool, WebFetchToolInput


@pytest.mark.asyncio
async def test_fixed_tool_keeps_schema_blocks_external_and_defers_revisions(tmp_path):
    case = next(c for c in load_cases() if c.id == "cross-syn-16")
    (tmp_path / "materials").mkdir()
    original = case.assets[0]
    shutil.copyfile(
        DEFAULT_DATASET / original.path, tmp_path / "materials" / original.path.split("/")[-1]
    )
    tool = FrozenExternalTool(WebFetchTool(), case.assets, tmp_path)
    assert tool.to_api_schema() == WebFetchTool().to_api_schema()
    context = ToolExecutionContext(tmp_path)
    late = next(a for a in case.assets if a.available_from_turn)
    assert (await tool.execute(WebFetchToolInput(url=late.locator), context)).is_error
    assert (await tool.execute(WebFetchToolInput(url="https://example.org"), context)).is_error
    result = await tool.execute(WebFetchToolInput(url=original.locator, max_chars=500), context)
    assert not result.is_error and len(result.output) == 500
    assert result.metadata["research_source_specs"]


@pytest.mark.asyncio
async def test_real_python_executes_and_cannot_read_gold_or_open_network(tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    gold = tmp_path / "gold.json"
    gold.write_text('{"answer": "never expose"}')
    store = ResearchStore(workspace, "a" * 12, root=workspace / "states")
    tool = RestrictedPythonTool(BashTool(), workspace, [])
    context = ToolExecutionContext(workspace, {"research_store": store})
    command = 'python -c \'from decimal import Decimal; print(Decimal("100") / Decimal("4"))\''
    result = await tool.execute(BashToolInput(command=command), context)
    assert not result.is_error and "25" in result.output
    forbidden = [
        "python -c 'from pathlib import Path; print(Path(\"../gold.json\").read_text())'",
        "python -c 'import socket; socket.create_connection((\"example.org\", 443))'",
        "cat ../gold.json",
        "python -m researchx.evaluation.dataset",
    ]
    for command in forbidden:
        result = await tool.execute(BashToolInput(command=command), context)
        assert result.is_error and "never expose" not in result.output
    exported = await tool.execute(
        BashToolInput(
            command=(
                "python -c 'from docx import Document; d=Document(); "
                'd.add_paragraph("真实导出报告"); d.save("report.docx")\''
            )
        ),
        context,
    )
    assert not exported.is_error
    from researchx.evaluation.artifacts import read_deliverable

    text, metadata = read_deliverable(workspace / "report.docx")
    assert "真实导出报告" in text and metadata["valid"]


@pytest.mark.asyncio
async def test_failed_sandbox_script_is_a_recoverable_failure_not_an_uncertain_side_effect(tmp_path):
    """A non-zero sandbox run must not be recorded as an uncertain write-["*"] operation.

    Regression: the default contract effect is "unknown"; a failing script then becomes
    "uncertain", and because serial parallelism declares write ["*"], every later tool in
    the session is blocked forever ("Unresolved side effects block this resource").
    """
    from researchx.tools.contracts import resolve_contract

    workspace = tmp_path / "work"
    workspace.mkdir()
    tool = RestrictedPythonTool(BashTool(), workspace, [])
    assert resolve_contract(tool).effect == "local_write"
    context = ToolExecutionContext(workspace)
    result = await tool.execute(BashToolInput(command="python -c 'raise SystemExit(2)'"), context)
    assert result.is_error and result.no_effect is None


@pytest.mark.asyncio
async def test_fault_occurrence_can_recover_without_registering_error_as_source(tmp_path):
    case = load_cases()[0]
    (tmp_path / "materials").mkdir()
    for a in case.assets:
        shutil.copyfile(DEFAULT_DATASET / a.path, tmp_path / "materials" / a.path.split("/")[-1])
    tool = FaultTool(
        FrozenExternalTool(WebFetchTool(), case.assets, tmp_path),
        [Fault(tool="web_fetch", kind="tool_error", recovery="重试原文")],
    )
    args = WebFetchToolInput(url=case.assets[0].locator)
    context = ToolExecutionContext(tmp_path)
    first = await tool.execute(args, context)
    second = await tool.execute(args, context)
    assert first.is_error and first.metadata["research_source_specs"] == []
    assert not second.is_error
    assert json.loads(second.output)["synthetic"] is True
