"""Paid, opt-in research evaluation through the real HTTP/WebSocket product.

python tests/test_web/real_research_eval.py --profile PROFILE --output /tmp/research-eval
The selected credentials are copied into a temporary isolated configuration and
deleted at exit. Provider, tool, storage and Web transports are never mocked.
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import httpx
import uvicorn
import websockets

import openharness.runtime as runtime
from openharness.api.client import ApiMessageCompleteEvent
from openharness.auth.storage import store_credential
from openharness.config.settings import Settings, save_settings
from openharness.research.store import ResearchStore
from openharness.research.models import ResearchMemory
from openharness.web.app import create_app
from openharness.web.catalog import profile_settings

MS_URL = "https://news.microsoft.com/source/2024/07/30/microsoft-cloud-strength-drives-fourth-quarter-results-6/"
APPLE_URL = "https://www.apple.com/newsroom/2024/10/apple-reports-fourth-quarter-results/"


class Evaluation:
    def __init__(self, output: Path, port: int, browser: str | None):
        self.output = output
        self.output.mkdir(parents=True, exist_ok=True)
        self.cwd = output / "workspace"
        self.cwd.mkdir(exist_ok=True)
        self.port = port
        self.base = f"http://127.0.0.1:{port}"
        self.browser = browser
        self.server = None
        self.server_task = None
        self.traces: list[dict] = []
        self.results: list[dict] = []
        self.tool_timings: list[dict] = []
        self.clarification = "仅研究问题中指定的历史财年和官方资料，按 GAAP 口径；不需要实时行情或投资建议。"
        self.client = httpx.AsyncClient(base_url=self.base, timeout=30, headers={"Origin": self.base})

    async def start(self):
        self.server = uvicorn.Server(uvicorn.Config(create_app(str(self.cwd)), host="127.0.0.1", port=self.port, log_level="warning"))
        self.server_task = asyncio.create_task(self.server.serve())
        async with asyncio.timeout(20):
            while not self.server.started:
                if self.server_task.done():
                    await self.server_task
                    raise RuntimeError("Web service failed to start")
                await asyncio.sleep(0.05)

    async def stop(self):
        if self.server:
            self.server.should_exit = True
            await self.server_task
            self.server = None

    async def new_session(self) -> str:
        response = await self.client.post("/api/sessions", json={"profile_id": "eval"})
        response.raise_for_status()
        return response.json()["session_id"]

    def memory(self, sid):
        return ResearchStore(self.cwd, sid).load()

    async def turn(self, name: str, sid: str, question: str, *, steer: str | None = None) -> dict:
        started = time.monotonic()
        request_id = uuid4().hex
        initial_id = request_id
        trace_start = len(self.traces)
        tool_start = len(self.tool_timings)
        progress = []
        errors = []
        prompts = []
        interruptions = []
        before = self.memory(sid)
        print(json.dumps({"case": name, "state": "START", "session_id": sid}, ensure_ascii=False), flush=True)
        async with asyncio.timeout(900):
            async with websockets.connect(f"ws://127.0.0.1:{self.port}/api/sessions/{sid}/ws", origin=self.base, max_size=4_000_000) as ws:
                ready = json.loads(await ws.recv())
                assert ready["type"] == "ready"
                await ws.send(json.dumps({"type": "submit", "request_id": request_id, "text": question}, ensure_ascii=False))
                steering_id = None
                while True:
                    event = json.loads(await ws.recv())
                    if event["type"] in {"tool_started", "tool_completed"}:
                        raise AssertionError("Internal tools leaked to the browser transport")
                    if event["type"] == "research_progress":
                        progress.append(event)
                        print(json.dumps({"case": name, "state": "PROGRESS", **event["progress"]}, ensure_ascii=False), flush=True)
                        memory = self.memory(sid)
                        if steer and not steering_id and memory.research_state.current_plan_id and any(source.kind == "web" and not source.is_error for source in memory.sources.values()):
                            steering_id = uuid4().hex
                            interruptions.append({"old_plan_id": memory.research_state.current_plan_id, "revision": memory.revision})
                            await ws.send(json.dumps({"type": "steer", "request_id": steering_id,
                                                      "target_request_id": initial_id, "text": steer}, ensure_ascii=False))
                    elif event["type"] == "steer_accepted":
                        request_id = event["next_request_id"]
                    elif event["type"] == "prompt":
                        prompts.append(event)
                        if event["kind"] == "question":
                            answer = self.clarification
                        else:
                            tool_name = event.get("tool_name", "")
                            answer = "allow" if tool_name == "bash" or tool_name.startswith(("mcp__ftshare__", "mcp__fuyao")) else "deny"
                        await ws.send(json.dumps({"type": "response", "request_id": event["request_id"],
                                                  "prompt_id": event["prompt_id"], "answer": answer}, ensure_ascii=False))
                    elif event["type"] in {"error", "rejected"}:
                        errors.append(event)
                    elif event["type"] == "done":
                        if event["request_id"] == request_id:
                            final = event
                            break
        response = await self.client.get(f"/api/sessions/{sid}")
        response.raise_for_status()
        view = response.json()
        answers = [row["text"] for row in view["messages"] if row["role"] == "assistant"]
        result = {"name": name, "session_id": sid, "question": question, "steering_question": steer,
                  "seconds": round(time.monotonic() - started, 2), "failed": final["failed"],
                  "cancelled": final["cancelled"], "answer": answers[-1] if answers else "",
                  "progress_events": progress, "errors": errors, "prompts": prompts, "interruptions": interruptions,
                  "before": before.model_dump(mode="json"), "after": self.memory(sid).model_dump(mode="json"),
                  "requests": self.traces[trace_start:], "tool_timings": self.tool_timings[tool_start:], "checks": []}
        self.results.append(result)
        self.check(result, "Web 执行完成且没有错误", not final["failed"] and not final["cancelled"] and not errors)
        self.check(result, "浏览器没有内部工具详情", all(row["role"] not in {"tool", "tool_result"} for row in view["messages"]))
        self.check(result, "动态记忆始终属于当前会话", all(item["memory"].get("session_id") == sid for item in result["requests"]))
        self.save()
        return result

    @staticmethod
    def check(result, label, condition):
        result["checks"].append({"label": label, "passed": bool(condition)})

    def research_checks(self, result):
        memory = result["after"]
        plan_id = memory["research_state"]["current_plan_id"]
        plan = memory["plans"].get(plan_id, {})
        self.check(result, "建立结构化目标和计划", bool(memory["current_context_id"] and plan))
        self.check(result, "所有计划任务已提交完成", bool(plan.get("tasks")) and all(task["status"] == "completed" for task in plan["tasks"]))
        self.check(result, "实际采集官方网页", any(source["kind"] == "web" and not source["is_error"] for source in memory["sources"].values()))
        self.check(result, "证据、论证和结论均已登记", bool(memory["evidence_pool"] and memory["reasoning_chain"] and memory["conclusions"]))
        self.check(result, "回答使用已冻结的有效来源", "来源：" in result["answer"] and "来源不可核验" not in result["answer"] and bool(memory["answers"]))

    def planning_order(self, result):
        history = result["after"]["history"]
        created = next((item["revision"] for item in history if item["action"] == "create_plan" and item["revision"] > result["before"]["revision"]), None)
        fetched = [item["revision"] for item in history if item["action"] == "capture_source"
                   and item["revision"] > result["before"]["revision"]
                   and result["after"]["sources"][item["data"]["source_id"]]["kind"] == "web"]
        self.check(result, "复杂研究先提交计划再采集", bool(created is not None and fetched and created < min(fetched)))

    def audit_loss_directions(self, result):
        memory = ResearchMemory.model_validate(result["after"])
        comparisons = []
        issues = []
        replaced = {item.supersedes for item in memory.evidence_pool.values()}
        companies_by_source = {}
        for source in memory.sources.values():
            if source.title == "ft_v1_finance_income" and not source.is_error:
                try:
                    data = json.loads(self.memory_store(result["session_id"]).read_source(source))
                    companies_by_source[source.id] = {row["stock_name"] for row in data.get("data", [])}
                except (ValueError, TypeError, KeyError):
                    continue
        known_companies = set().union(*companies_by_source.values()) if companies_by_source else set()
        provenance_issues = []
        for evidence in memory.evidence_pool.values():
            if evidence.status == "retracted" or evidence.id in replaced:
                continue
            source = memory.sources[evidence.source_id]
            if source.title != "ft_v1_finance_income":
                continue
            allowed = set(companies_by_source.get(source.id, set()))
            for key in evidence.input_evidence_ids:
                allowed.update(companies_by_source.get(memory.evidence_pool[key].source_id, set()))
            if any(company in evidence.statement and company not in allowed for company in known_companies):
                provenance_issues.append(evidence.id)
            try:
                rows = json.loads(self.memory_store(result["session_id"]).read_source(source)).get("data", [])
            except (ValueError, TypeError):
                continue
            field = "parcomp_n_profit" if "归母" in evidence.statement else "n_profit"
            half_years = [row for row in rows if row.get("report_type") == "q2"]
            if not half_years:
                continue
            current = max(half_years, key=lambda row: row["year"])
            group = str(current.get("report_form_type", ""))[:2]
            prior = next((row for row in half_years if row["year"] == current["year"] - 1
                          and row.get("stock_code") == current.get("stock_code")
                          and str(row.get("report_form_type", ""))[:2] == group), None)
            if not prior or current.get(field) is None or prior.get(field) is None:
                continue
            now_value, previous_value = float(current[field]), float(prior[field])
            if now_value >= 0 or previous_value >= 0:
                continue
            change = abs(now_value) / abs(previous_value) - 1
            comparisons.append({"evidence_id": evidence.id, "source_id": source.id,
                "company": current["stock_name"], "field": field, "current": now_value,
                "prior": previous_value, "loss_change_percent": round(change * 100, 3),
                "current_form": current.get("report_form_type"), "prior_form": prior.get("report_form_type")})
            statement = re.split(r"旧记录|原记录|原说法|之前记录|此前记录", evidence.statement, maxsplit=1)[0]
            def asserted(pattern):
                for match in re.finditer(pattern, statement):
                    prefix = re.split(r"[。；，,:：()（）]", statement[:match.start()])[-1]
                    if not re.search(r"不能|不可|不应|不代表|不等于|并非|非|未能|无法|不宜", prefix):
                        return True
                return False
            claims_reduction = asserted(r"减亏|亏损.{0,12}(?:收窄|缩小|减少)")
            claims_increase = asserted(r"增亏|亏损.{0,12}(?:扩大|增加)")
            if (claims_reduction and change > 0.001) or (claims_increase and change < -0.001):
                issues.append(evidence.id)
            if field == "parcomp_n_profit":
                amount_claim = re.search(r"归母(?:亏损|净利)(?:绝对额)?[^。；]*?扩大([^。；]*)", statement)
                if amount_claim:
                    tail = amount_claim.group(1)
                    amount = re.match(r"\s*(\d+(?:\.\d+)?)\s*亿(?:元)?", tail)
                    percent = re.search(r"([+-]?\d+(?:\.\d+)?)\s*%", tail)
                    delta = (abs(now_value) - abs(previous_value)) / 1e8
                    if amount and abs(float(amount.group(1)) - delta) > 0.02:
                        issues.append(evidence.id)
                    if percent and abs(float(percent.group(1)) - change * 100) > 0.15:
                        issues.append(evidence.id)
        result["financial_comparisons"] = comparisons
        result["financial_audit_issues"] = sorted(set(issues))
        result["financial_provenance_issues"] = provenance_issues
        self.check(result, "亏损方向、差额及幅度与原始利润表一致", not issues)
        self.check(result, "财务证据分别关联对应公司来源", not provenance_issues)

    def audit_recent_spot_prices(self, result):
        memory = ResearchMemory.model_validate(result["after"])
        prices = []
        replaced = {item.supersedes for item in memory.evidence_pool.values()}
        for evidence in memory.evidence_pool.values():
            source = memory.sources[evidence.source_id]
            if evidence.status == "retracted" or evidence.id in replaced or source.kind != "web" or source.is_error:
                continue
            if not re.search(r"硅料|多晶硅|硅片|电池片|组件", evidence.statement):
                continue
            # Article publication alone cannot date an embedded historical quote.
            date_match = re.search(r"(20\d{2})[-年/](\d{1,2})[-月/](\d{1,2})", evidence.period)
            if not date_match:
                continue
            quote_date = datetime(*map(int, date_match.groups()), tzinfo=timezone.utc)
            collection = datetime.fromisoformat(source.collected_at.replace("Z", "+00:00"))
            if not 0 <= (collection - quote_date).days <= 45:
                continue
            raw = self.memory_store(result["session_id"]).read_source(source)
            # Require actual manufacturing spot units in the immutable original;
            # export USD/kg averages or futures yuan/tonne are insufficient.
            numeric_unit = r"\d+(?:\.\d+)?\s*(?:元|美元|USD)[/／每]\s*(?:[Ww]|瓦|[Kk][Gg]|公斤|片)"
            chinese_unit = r"每(?:公斤|瓦|片)\s*\d+(?:\.\d+)?(?:\s*[-—–]\s*\d+(?:\.\d+)?)?\s*(?:元|美元)"
            table_unit = r"(?:Cell|Module|Polysilicon|Wafer).{0,50}[（(](?:W|kg|pc)[）)]"
            if re.search(numeric_unit, raw) or re.search(chinese_unit, raw) or (
                re.search(table_unit, raw) and re.search(numeric_unit, evidence.statement)
                and re.search(r"(?:RMB|USD)[）)]\s*\d+\.\d+", raw)
            ):
                prices.append(evidence.id)
        result["recent_spot_evidence_ids"] = prices
        self.check(result, "已登记近45天产业链现货原文报价", bool(prices))

    def memory_store(self, sid):
        return ResearchStore(self.cwd, sid)

    def save(self):
        payload = {"model": getattr(self, "model", ""), "cases": self.results, "browser": getattr(self, "browser_result", None)}
        (self.output / "report.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        rows = []
        for result in self.results:
            checks = "".join(f"<li>{'✓' if item['passed'] else '✗'} {html.escape(item['label'])}</li>" for item in result["checks"])
            rows.append(f"<section><h2>{html.escape(result['name'])}</h2><p>{html.escape(result['question'])}</p>"
                        + (f"<p>打断修改：{html.escape(result['steering_question'])}</p>" if result["steering_question"] else "")
                        + f"<ul>{checks}</ul><details open><summary>实际回答（{result['seconds']} 秒）</summary><pre>{html.escape(result['answer'])}</pre></details></section>")
        (self.output / "report.html").write_text(
            '<!doctype html><meta charset="utf-8"><title>真实投研端到端测试</title><style>body{max-width:1100px;margin:35px auto;font:16px/1.65 system-ui;background:#f5f5f5}section{background:white;padding:24px;margin:20px 0;border-radius:10px}pre{white-space:pre-wrap;font:inherit}h1,h2{line-height:1.3}</style>'
            + f"<h1>真实投研端到端测试 · {html.escape(getattr(self, 'model', ''))}</h1>" + "".join(rows), encoding="utf-8")

    async def browser_check(self, sid: str):
        if not self.browser:
            return
        code = """
import { chromium } from 'playwright';
import { expect } from '@playwright/test';
const browser = await chromium.launch({headless:true, executablePath:process.env.EVAL_BROWSER});
try {
 const page=await browser.newPage({viewport:{width:1440,height:1100}});
 const errors=[]; page.on('pageerror',e=>errors.push(e.message));
 await page.addInitScript(id=>sessionStorage.setItem('openharness.web.session',id),process.env.EVAL_SESSION);
 await page.goto(process.env.EVAL_BASE);
 await expect(page.getByLabel('研究任务进度')).toBeVisible({timeout:20000});
 await expect(page.locator('.message.assistant').last()).toContainText('来源：');
 await expect(page.locator('.tool-row')).toHaveCount(0);
 await expect(page.locator('.message-list')).not.toContainText('research_memory');
 const before=await page.getByLabel('研究任务进度').textContent();
 await page.reload();
 await expect(page.getByLabel('研究任务进度')).toHaveText(before);
 await expect(page.getByRole('button',{name:'停止生成'})).toHaveCount(0);
 const answer=page.locator('.message.assistant').last();
 await answer.scrollIntoViewIfNeeded();
 await expect(answer).toBeVisible();
 await page.locator('.conversation-scroll').evaluate(element=>element.scrollTo({top:element.scrollHeight,behavior:'instant'}));
 expect(errors).toEqual([]);
 await page.screenshot({path:process.env.EVAL_SCREENSHOT,fullPage:true});
 console.log(JSON.stringify({passed:true,restored_progress:before}));
} finally { await browser.close(); }
"""
        env = dict(os.environ, EVAL_BROWSER=self.browser, EVAL_SESSION=sid, EVAL_BASE=self.base,
                   EVAL_SCREENSHOT=str(self.output / "browser.png"))
        proc = await asyncio.create_subprocess_exec("node", "--input-type=module", "-e", code,
            cwd=Path(__file__).resolve().parents[2] / "frontend/web", env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await proc.communicate()
        self.browser_result = {"passed": proc.returncode == 0, "stdout": stdout.decode(), "stderr": stderr.decode()}
        self.save()


async def main(args):
    selected = profile_settings(args.profile)
    credential = selected.resolve_auth().value
    original = runtime._resolve_api_client_from_settings
    evaluation = Evaluation(args.output.resolve(), args.port, args.browser)
    evaluation.model = selected.model
    import openharness.engine.query as query_module
    execute_tool = query_module._execute_tool_call

    async def timed_tool(context, name, call_id, arguments):
        started = time.monotonic()
        item = {"name": name, "id": call_id, "started_at": time.time()}
        evaluation.tool_timings.append(item)
        try:
            result = await execute_tool(context, name, call_id, arguments)
            item.update(is_error=result.is_error, output_preview=result.content[:700] if result.is_error else "")
            return result
        finally:
            item["seconds"] = round(time.monotonic() - started, 2)
            print(json.dumps({"state": "TOOL", **item}, ensure_ascii=False), flush=True)

    query_module._execute_tool_call = timed_tool

    class TracedProvider:
        def __init__(self, provider):
            self.provider = provider

        async def close(self):
            await self.provider.close()

        def __getattr__(self, name):
            return getattr(self.provider, name)

        async def stream_message(self, request):
            started = time.monotonic()
            context = next((message.runtime_context for message in reversed(request.messages) if message.runtime_context), "")
            match = re.search(r"<research_memory>\n(.*?)\n</research_memory>", context, re.S)
            trace = {"memory": json.loads(match.group(1)) if match else {}, "tools": [], "usage": {},
                     "previous_tool_errors": [{"tool_use_id": block.tool_use_id, "message": block.content}
                        for message in request.messages[-2:] for block in message.content
                        if getattr(block, "type", "") == "tool_result" and block.is_error]}
            evaluation.traces.append(trace)
            async for event in self.provider.stream_message(request):
                if isinstance(event, ApiMessageCompleteEvent):
                    trace["seconds"] = round(time.monotonic() - started, 2)
                    trace["tools"] = [{"name": tool.name, "input": tool.input} for tool in event.message.tool_uses]
                    trace["usage"] = event.usage.model_dump()
                    print(json.dumps({"state": "MODEL", "tools": [tool["name"] for tool in trace["tools"]]}, ensure_ascii=False), flush=True)
                yield event

    runtime._resolve_api_client_from_settings = lambda settings: TracedProvider(original(settings))
    with TemporaryDirectory(prefix="research-eval-config-") as config:
        os.environ["OPENHARNESS_CONFIG_DIR"] = config
        os.environ["OPENHARNESS_DATA_DIR"] = str(evaluation.output / "data")
        profile = selected.resolve_profile()[1].model_copy(update={"credential_slot": "eval", "label": "真实投研测试"})
        isolated = Settings(active_profile="eval", profiles={"eval": profile}, api_format=selected.api_format,
                            model=selected.model, base_url=selected.base_url, provider=selected.provider,
                            web=selected.web.model_copy(deep=True),
                            mcp_servers=selected.mcp_servers if args.with_mcp else {})
        save_settings(isolated)
        store_credential("profile:eval", "api_key", credential, use_keyring=False)
        try:
            await evaluation.start()
            if args.question:
                evaluation.clarification = "中国光伏产业链，截至今天，依据可取得的公开资料分析；缺失数据明确标注，不需要买卖建议。"
                sid = await evaluation.new_session()
                result = await evaluation.turn("原始问题复现", sid, args.question)
                evaluation.check(result, "采集资料并登记证据", bool(result["after"]["evidence_pool"]))
                current_plan = result["after"]["plans"].get(result["after"]["research_state"]["current_plan_id"], {})
                evaluation.check(result, "研究结束后没有遗留待执行或执行中任务", bool(current_plan.get("tasks")) and all(
                    task["status"] in {"completed", "blocked", "cancelled"} for task in current_plan["tasks"]))
                evaluation.check(result, "采集前已提交执行中任务", any(
                    history["action"] == "update_task" and history["data"].get("status") == "in_progress"
                    for history in result["after"]["history"]))
                evaluation.check(result, "最终回答具有存储生成的来源", "来源：" in result["answer"] and "来源不可核验" not in result["answer"])
                if args.require_industry_data:
                    records = list(result["after"]["evidence_pool"].values())
                    statements = [record["statement"] for record in records if record["status"] != "retracted"]
                    evaluation.audit_recent_spot_prices(result)
                    evaluation.check(result, "已登记光伏装机或出口的产业需求证据", any(
                        re.search(r"光伏|太阳能|组件", statement) and re.search(r"装机|出口", statement)
                        and re.search(r"\d+(?:\.\d+)?\s*(?:GW|吉瓦|万千瓦|亿千瓦|%|％)", statement)
                        for statement in statements))
                    evaluation.check(result, "输出包括已保存论证及结论", bool(result["after"]["reasoning_chain"] and result["after"]["conclusions"]))
                    evaluation.audit_loss_directions(result)
                await evaluation.stop()
                await evaluation.start()
                followup = await evaluation.turn("重启后复核资料时效", sid,
                    "沿用刚才研究，不再联网。列出已有资料各自的日期，哪些足以判断当前景气度，哪些过旧或缺失？不要把旧数据说成当前。")
                evaluation.check(followup, "追问保留研究目标", result["after"]["current_context_id"] == followup["after"]["current_context_id"])
                if args.browser:
                    await evaluation.browser_check(sid)
                evaluation.save()
                passed = all(check["passed"] for case in evaluation.results for check in case["checks"])
                passed = passed and (not args.browser or evaluation.browser_result["passed"])
                print(json.dumps({"result": "PASS" if passed else "ISSUES", "session_id": sid, "report": str(evaluation.output / "report.html")}), flush=True)
                return
            if args.browser_only_session:
                saved = json.loads((evaluation.output / "report.json").read_text(encoding="utf-8"))
                evaluation.results = saved["cases"]
                await evaluation.browser_check(args.browser_only_session)
                print(json.dumps({"browser_passed": evaluation.browser_result["passed"]}), flush=True)
                return
            if args.resume_research_session:
                saved = json.loads((evaluation.output / "report.json").read_text(encoding="utf-8"))
                evaluation.results = saved["cases"]
                sid = args.resume_research_session
                result = await evaluation.turn("同会话量化复核与更正", sid,
                    "继续刚才的光伏行业研究。请从原始资料重新核对报告中所有财务同比、亏损差额与幅度，"
                    "通过实际执行计算核验，不要心算。若发现错误，修订证据、论证和结论并保留历史版本，"
                    "撤回错误记录对结论的影响。然后输出完整的修正报告，包含近期现货报价、产业需求、"
                    "盈利与数据限制。原范围内直接完成，不需要再问我是否继续。")
                evaluation.research_checks(result)
                evaluation.audit_loss_directions(result)
                evaluation.audit_recent_spot_prices(result)
                if args.browser:
                    await evaluation.browser_check(sid)
                evaluation.save()
                passed = all(check["passed"] for check in result["checks"])
                passed = passed and (not args.browser or evaluation.browser_result["passed"])
                print(json.dumps({"result": "PASS" if passed else "ISSUES", "session_id": sid,
                                  "report": str(evaluation.output / "report.html")}), flush=True)
                return
            if args.followup_session:
                saved = json.loads((evaluation.output / "report.json").read_text(encoding="utf-8"))
                evaluation.results = saved["cases"]
                result = await evaluation.turn("08 苹果归因边界复核与结论修订", args.followup_session,
                    "你刚才说苹果 Q4 的 EPS 差异来自一次性税收费用，‘而非经营恶化’。只凭这份新闻稿，"
                    "真的能排除经营恶化吗？请复核并撤回或修订任何超出证据的结论，沿用本研究目标和已有资料，"
                    "区分能证明、只能推断和仍不能确定的事项，不要新查资料。600 字以内。")
                evaluation.research_checks(result)
                evaluation.check(result, "边界追问保留研究目标", result["before"]["current_context_id"] == result["after"]["current_context_id"])
                evaluation.check(result, "复核后明确不能排除经营变化", bool(re.search(r"(?:不能|无法|不足以|不应|不可以|不等于).{0,30}(?:排除|经营恶化|经营变化)", result["answer"])))
                old_ids = set(result["before"]["conclusions"])
                evaluation.check(result, "结论修订保留原版本", any(claim["supersedes"] in old_ids for key, claim in result["after"]["conclusions"].items() if key not in old_ids))
                await evaluation.browser_check(args.followup_session)
                evaluation.save()
                print(json.dumps({"result": "PASS" if all(check["passed"] for check in result["checks"]) else "ISSUES", "followup": result["name"]}, ensure_ascii=False), flush=True)
                return
            first = await evaluation.new_session()
            a = await evaluation.turn("01 微软财年盈利质量", first,
                f"请研究微软 FY2024 盈利质量，只用这份官方资料：{MS_URL}。比较 FY2024 和 FY2023 的 GAAP 营收、净利润和净利率，"
                "计算营收同比、净利率变动（百分点），百分数保留两位小数。区分事实与推断，列出两个还需要核验的风险。"
                "不要使用实时行情，也不要给买卖建议。回答控制在 800 字以内。")
            evaluation.research_checks(a)
            evaluation.planning_order(a)
            evaluation.check(a, "同比与净利率计算正确", percentages(a["answer"], [15.67, 35.96, 34.15, 1.81]))
            await evaluation.stop()
            await evaluation.start()
            b = await evaluation.turn("02 服务重启后的情景计算", first,
                "沿用刚才微软研究的目标和资料，假设 FY2024 收入不变、净利润下降 10%，净利率会变成多少，下降多少个百分点？"
                "百分数保留两位小数。这只是情景假设，不是预测。无需重新查网页，回答在 400 字以内。")
            evaluation.research_checks(b)
            evaluation.check(b, "追问保留研究目标", b["before"]["current_context_id"] == b["after"]["current_context_id"])
            evaluation.check(b, "恢复后没有重新采集网页", not any(tool["name"] == "web_fetch" for request in b["requests"] for tool in request["tools"]))
            evaluation.check(b, "情景净利率与变动正确", percentages(b["answer"], [32.36, 3.60]))
            c = await evaluation.turn("03 用户转述与官方数据冲突", first,
                "我收到一条没有出处的内部转述，说微软 FY2024 营收其实是 2500 亿美元。请将它与已有官方数据比较；"
                "不要直接当成事实覆盖原结论。说明差异和待核验事项，回答在 400 字以内。")
            evaluation.research_checks(c)
            user_sources = {key for key, source in c["after"]["sources"].items() if source["kind"] == "user" and key not in c["before"]["sources"]}
            evaluation.check(c, "用户转述作为待核验资料登记", any(ev["source_id"] in user_sources and ev["status"] == "pending" for ev in c["after"]["evidence_pool"].values()))
            d = await evaluation.turn("04 同一对话换题为苹果", first,
                f"换个研究对象，研究苹果 FY2024 第四季度，只用 {APPLE_URL}。收入增长与每股收益为何可能表现不同？"
                "明确区分 GAAP EPS 和剔除一次性项目的 EPS，以及季度和全年口径；哪些结论尚不能确定？不要引用微软资料，800 字以内。")
            evaluation.research_checks(d)
            evaluation.planning_order(d)
            old_plan = d["before"]["research_state"]["current_plan_id"]
            evaluation.check(d, "换题归档旧计划并提交新计划", bool(old_plan and d["after"]["plans"][old_plan]["archived"] and d["after"]["research_state"]["current_plan_id"] != old_plan))
            evaluation.check(d, "新计划未隐式复用微软证据", not d["after"]["plans"].get(d["after"]["research_state"]["current_plan_id"], {}).get("reused_evidence_ids"))
            second = await evaluation.new_session()
            e = await evaluation.turn("05 新对话隔离", second,
                "继续研究微软，但这次先不要联网或查文件。你能读取上一个对话里保存的证据和情景假设吗？"
                "只说明本对话当前有哪些资料，没有就说明没有，不要凭记忆补数据。")
            other_ids = set(d["after"]["evidence_pool"]) | set(d["after"]["sources"])
            evaluation.check(e, "新会话没有其他会话证据", not e["after"]["evidence_pool"])
            evaluation.check(e, "所有注入与答案均无其他会话 ID", not any(key in json.dumps(e["requests"], ensure_ascii=False) + e["answer"] for key in other_ids))
            third = await evaluation.new_session()
            f = await evaluation.turn("06 真实网页检索", third,
                f"先搜索微软 FY2024 全年营收的官方披露，再读取原文，回答收入是多少、期间何时结束。搜索摘要不能直接当财报原文。"
                f"如果搜索不可用，可以直接读取官方页面 {MS_URL}，并说明检索限制。500 字以内。")
            evaluation.check(f, "读取官方原文并登记证据", any(source["kind"] == "web" and not source["is_error"] for source in f["after"]["sources"].values()) and bool(f["after"]["evidence_pool"]))
            evaluation.check(f, "简单资料问答附有效来源", "来源：" in f["answer"] and "来源不可核验" not in f["answer"])
            evaluation.check(f, "实际执行了网页搜索", any(tool["name"] == "web_search" for request in f["requests"] for tool in request["tools"]))
            fourth = await evaluation.new_session()
            g = await evaluation.turn("07 采集后打断并重新规划", fourth,
                f"对微软 FY2024 做完整研究，依据 {MS_URL} 收集财务数据，再分析营收、利润和股东回报，列出进一步研究事项。800 字以内。",
                steer=f"打断并修改：范围收窄为只核对微软 FY2024 全年 GAAP 净利润与 FY2023，依据 {MS_URL}。给出同比增长，不要继续股东回报分析。400 字以内。")
            evaluation.research_checks(g)
            evaluation.check(g, "资料采集后已接收修改要求", bool(g["interruptions"]))
            if g["interruptions"]:
                archived = g["interruptions"][0]["old_plan_id"]
                evaluation.check(g, "旧计划归档且新计划生效", bool(archived and g["after"]["plans"].get(archived, {}).get("archived") and g["after"]["research_state"]["current_plan_id"] != archived))
                evaluation.check(g, "旧计划未继续完成被取消任务", any(task["status"] == "cancelled" for task in g["after"]["plans"].get(archived, {}).get("tasks", [])))
            await evaluation.browser_check(first)
            evaluation.save()
            summary = {"result": "PASS" if all(check["passed"] for result in evaluation.results for check in result["checks"]) and (not evaluation.browser or evaluation.browser_result["passed"]) else "ISSUES",
                       "cases": [{"name": result["name"], "passed": all(check["passed"] for check in result["checks"]),
                                  "failed_checks": [check["label"] for check in result["checks"] if not check["passed"]]} for result in evaluation.results],
                       "report": str(evaluation.output / "report.html")}
            print(json.dumps(summary, ensure_ascii=False), flush=True)
        finally:
            evaluation.save()
            await evaluation.stop()
            await evaluation.client.aclose()
            runtime._resolve_api_client_from_settings = original
            query_module._execute_tool_call = execute_tool


def percentages(text: str, values: list[float]) -> bool:
    found = [float(value) for value in re.findall(r"(-?\d+(?:\.\d+)?)\s*(?:%|％|个百分点|pp)", text)]
    return all(any(abs(abs(actual) - expected) <= 0.03 for actual in found) for expected in values)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--browser", default=os.environ.get("OPENHARNESS_TEST_BROWSER"))
    parser.add_argument("--followup-session", help="Append a critical review to an existing evaluation session")
    parser.add_argument("--resume-research-session", help="Audit and correct an existing research through the real model")
    parser.add_argument("--browser-only-session", help="Inspect a saved session in the production browser without model calls")
    parser.add_argument("--question", help="Run an unmodified research question and a recovery follow-up")
    parser.add_argument("--with-mcp", action="store_true", help="Use the selected profile's actual MCP servers")
    parser.add_argument("--require-industry-data", action="store_true", help="Require actual solar chain prices and demand evidence for the photovoltaic scenario")
    args = parser.parse_args()
    try:
        asyncio.run(main(args))
    except Exception as exc:
        print(json.dumps({"result": "FAILED_RUN", "error_type": type(exc).__name__}), flush=True)
        raise SystemExit(1) from None
