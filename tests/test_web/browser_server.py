"""Isolated Playwright server. Only the model transport is replaced for tests."""

from __future__ import annotations

import asyncio
import os
import tempfile
import json
import re
import sys
import shlex
import shutil
from pathlib import Path

import uvicorn

import openharness.runtime as agent_runtime
import openharness.web.app as web_app
from openharness.api.client import ApiMessageCompleteEvent, ApiTextDeltaEvent
from openharness.api.usage import UsageSnapshot
from openharness.config import Settings, save_settings
from openharness.engine.messages import ConversationMessage, TextBlock, ToolUseBlock
from research_model import ResearchModel


class BrowserModel:
    def __init__(self):
        self.research = None

    async def close(self):
        pass

    async def stream_message(self, request):
        last = next(message for message in reversed(request.messages) if message.content)
        user_text = next((message.text for message in reversed(request.messages) if message.role == "user" and message.text), "")
        if user_text in {"开展测试研究", "慢速研究公司 A", "改为研究公司 B"}:
            if self.research is None:
                context = next(message.runtime_context for message in reversed(request.messages) if message.runtime_context)
                data = json.loads(re.search(r"<research_memory>\n(.*?)\n</research_memory>", context, re.S).group(1))
                self.research = ResearchModel(WORKSPACE, data["session_id"], slow=user_text == "慢速研究公司 A",
                    title="公司 B 简要研究" if user_text == "改为研究公司 B" else "公司 A 简要研究")
            async for event in self.research.stream_message(request):
                yield event
            return
        if user_text == "查看执行过程":
            if last.role == 'user' and last.text:
                text = '我先读取两份资料，再整理结论。'
                yield ApiTextDeltaEvent(text)
                message = ConversationMessage(role='assistant', content=[
                    TextBlock(text=text),
                    ToolUseBlock(id='process-read-ok', name='read_file', input={'path': 'report.txt'}),
                    ToolUseBlock(id='process-read-fail', name='read_file', input={'path': 'missing.txt'}),
                ])
            else:
                # Keep the process running long enough to exercise disclosure controls.
                await asyncio.sleep(3)
                text = '已整理资料；第二份文件不可用。'
                yield ApiTextDeltaEvent(text)
                message = ConversationMessage(role='assistant', content=[TextBlock(text=text)])
            yield ApiMessageCompleteEvent(message=message, usage=UsageSnapshot(input_tokens=10, output_tokens=5))
            return
        if last.text.startswith("导出固定验收产物"):
            script = Path(__file__).parents[2] / "src/openharness/plugins/bundled/research-report-digest/skills/research-report-digest/scripts/run.py"
            command = f"{shlex.quote(sys.executable)} {shlex.quote(str(script))} digest --input fixtures/digest.json --output-dir browser-artifacts"
            message = ConversationMessage(role="assistant", content=[ToolUseBlock(name="bash", input={"command": command})])
        elif last.text == "请写入测试文件":
            message = ConversationMessage(role="assistant", content=[
                ToolUseBlock(name="write_file", input={"path": "approval.txt", "content": "测试"}),
            ])
        elif last.text == "请提问":
            message = ConversationMessage(
                role="assistant",
                content=[
                    ToolUseBlock(
                        name="ask_user_question", input={"question": "你希望研究哪个时间范围？"}
                    ),
                ],
            )
        else:
            text = "这是浏览器测试回复。\n\n| 项目 | 状态 |\n| --- | --- |\n| 对话链路 | 已接通 |"
            if last.text == "请显示测试标记":
                text = '<script>window.__unsafe=true</script>\n<img src="bad" onerror="window.__unsafe=true">\n\n安全内容\n\n[危险链接](javascript:alert(1))'
            if last.text == "请慢慢回复":
                yield ApiTextDeltaEvent("这是部分回复")
                await asyncio.sleep(3600)
            for chunk in (text[:8], text[8:18], text[18:]):
                yield ApiTextDeltaEvent(chunk)
                await asyncio.sleep(0.1)
            message = ConversationMessage(role="assistant", content=[TextBlock(text=text)])
        yield ApiMessageCompleteEvent(
            message=message, usage=UsageSnapshot(input_tokens=10, output_tokens=5, cache_read_input_tokens=0, cache_observed_input_tokens=10)
        )


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="openharness-browser-") as directory:
        root = Path(directory)
        os.environ["OPENHARNESS_CONFIG_DIR"] = str(root / "config")
        os.environ["OPENHARNESS_DATA_DIR"] = str(root / "data")
        for name in (
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "OPENHARNESS_OPENAI_API_KEY",
            "OPENHARNESS_ANTHROPIC_API_KEY",
            "OPENHARNESS_PROFILE",
        ):
            os.environ.pop(name, None)
        save_settings(Settings(memory={"enabled": False}))
        (root / "workspace").mkdir()
        WORKSPACE = root / "workspace"
        shutil.copytree(Path(__file__).parents[1] / "fixtures/research_skills", WORKSPACE / "fixtures")
        (WORKSPACE / "report.txt").write_text("测试资料：营业收入同比增长。", encoding="utf-8")
        agent_runtime._resolve_api_client_from_settings = lambda settings: BrowserModel()
        web_app._resolve_api_client_from_settings = lambda settings: BrowserModel()
        uvicorn.run(web_app.create_app(str(root / "workspace")), host="127.0.0.1", port=8765)
