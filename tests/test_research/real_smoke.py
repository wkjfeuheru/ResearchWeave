"""Opt-in real-provider research smoke; never collected by the unit test suite.

Run: python tests/test_research/real_smoke.py --profile PROFILE --workspace /tmp/eval-repo
Use a disposable external workspace. Credentials are loaded from existing profiles.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from tempfile import mkdtemp

from openharness.engine.stream_events import AssistantTurnComplete, ErrorEvent, ToolExecutionCompleted
from openharness.runtime import build_runtime, close_runtime, start_runtime
from openharness.web.runtime import RESEARCH_PROMPT
from openharness.web.storage import WebSessionBackend


async def run(profile: str, workspace: Path) -> None:
    if not workspace.is_dir():
        raise ValueError("An existing disposable workspace is required")
    os.environ["OPENHARNESS_DATA_DIR"] = mkdtemp(prefix="oh-research-smoke-data-")
    (workspace / "research-report.txt").write_text(
        "仅供测试的虚构数据：示例公司 2024 年营业收入 100 亿元，2025 年营业收入 120 亿元。\n"
        "本文无发布日期，无独立核验资料，不能作为投资依据。\n", encoding="utf-8",
    )
    backend = WebSessionBackend(str(workspace))
    session = backend.create(profile)
    bundle = None
    calls: list[str] = []
    errors: list[str] = []
    replies: list[str] = []
    try:
        for index, prompt in enumerate([
            "请研究 research-report.txt 中的虚构示例公司。创建两步任务概要（读取资料、分析变化），"
            "自动执行。实际调用工具读取文件，登记证据、论证摘要和暂定结论，并更新任务进度。"
            "先读研究记忆获取版本和用户消息来源 ID；每次写入按最新 revision 串行提交。"
            "收入数据的事实陈述带证据标记；发布日期未知，禁止标成已核验。只需用文件和记忆工具。",
            "沿用本对话的研究目标、证据和计划，回答 2025 年相对 2024 年的营业收入增长率。"
            "通过研究记忆读取已有证据，登记计算方法与结果的论证摘要及暂定结论，"
            "不要重新读取文件。答案带已有证据标记，保留虚构资料和未核验限制。",
        ]):
            record = backend.load_by_id(workspace, session["session_id"])
            bundle = await build_runtime(
                cwd=str(workspace), active_profile=profile, system_prompt=RESEARCH_PROMPT,
                session_id=session["session_id"],
                restore_messages=record["messages"], restore_usage=record["usage"],
                restore_tool_metadata=record["tool_metadata"],
            )
            # Scope this paid smoke to local evidence and memory. Runtime/MCP
            # lifecycle remains real, but unrelated tool schemas need no tokens.
            for tool in bundle.tool_registry.list_tools():
                if tool.name not in {"research_memory", "read_file", "glob"}:
                    bundle.tool_registry.unregister(tool.name)
            await start_runtime(bundle)
            turn_tools: list[str] = []
            async with asyncio.timeout(600):
                async for event in bundle.engine.submit_message(prompt):
                    if isinstance(event, ToolExecutionCompleted):
                        calls.append(event.tool_name)
                        turn_tools.append(event.tool_name)
                        if event.is_error:
                            errors.append(event.tool_name)
                        print(json.dumps({"turn": index + 1, "tool": event.tool_name, "error": event.is_error}), flush=True)
                    elif isinstance(event, ErrorEvent):
                        # Provider exceptions can contain credentials; keep their text out of reports.
                        raise RuntimeError("Provider returned an error event")
                    elif isinstance(event, AssistantTurnComplete) and not event.message.tool_uses:
                        replies.append(event.message.text)
            memory = bundle.engine.tool_metadata["research_store"].load()
            assert memory.current_context_id and memory.research_state.current_plan_id
            assert memory.evidence_pool and memory.reasoning_chain and memory.conclusions
            assert "research_memory" in turn_tools
            if index == 0:
                assert "read_file" in turn_tools
            else:
                assert "read_file" not in turn_tools
            progress = bundle.engine.tool_metadata["research_store"].progress()
            assert progress["completed"] == progress["total"] == 2
            assert "来源：" in replies[-1] and "research-report.txt" in replies[-1]
            assert all(claim.status != "verified" for claim in memory.conclusions.values())
            backend.save_snapshot(
                cwd=workspace, session_id=session["session_id"], model=bundle.engine.model,
                system_prompt=bundle.engine.system_prompt, messages=bundle.engine.messages,
                usage=bundle.engine.total_usage, tool_metadata=bundle.engine.tool_metadata,
            )
            print(json.dumps({"turn": index + 1, "completed": progress["completed"],
                              "revision": memory.revision, "usage": bundle.engine.total_usage.model_dump()}), flush=True)
            await close_runtime(bundle)
            bundle = None
        assert any("20%" in reply or "20％" in reply for reply in replies)
        print(json.dumps({"result": "PASS", "turns": 2, "tools": calls, "recovered_tool_errors": errors,
                          "data_directory": os.environ["OPENHARNESS_DATA_DIR"]}), flush=True)
    finally:
        if bundle is not None:
            await close_runtime(bundle)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    args = parser.parse_args()
    try:
        asyncio.run(run(args.profile, args.workspace.resolve()))
    except Exception as exc:
        print(json.dumps({"result": "FAIL", "exception_type": type(exc).__name__}), flush=True)
        raise SystemExit(1) from None
