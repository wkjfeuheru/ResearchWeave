"""Structured search regressions; no credentials or paid requests required."""

import asyncio

import httpx
import pytest

from researchx.tools.base import ToolExecutionContext
from researchx.tools.web_search_tool import WebSearchTool, WebSearchToolInput
from researchx.api import tavily_search as tavily


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("RESEARCHX_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.delenv("RESEARCHX_WEB_SEARCH_URL", raising=False)
    monkeypatch.delenv("RESEARCHX_TAVILY_API_KEY", raising=False)
    monkeypatch.setenv("TAVILY_API_KEY", "test-tavily-secret")
    monkeypatch.setattr(tavily, "load_credential", lambda *args: None)


def install(monkeypatch, data=None, status=200):
    calls = []

    async def fetch(url, **kwargs):
        calls.append((url, kwargs))
        return httpx.Response(
            status,
            json=data if data is not None else {"results": []},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(tavily, "fetch_public_http_response", fetch)
    return calls


def item(url, title="资料"):
    return {"url": url, "title": title, "content": "待核验摘要", "published_date": "2026-10-01"}


async def execute(tmp_path, **kwargs):
    return await WebSearchTool().execute(
        WebSearchToolInput(query="光伏 装机 累计 同比", **kwargs),
        ToolExecutionContext(cwd=tmp_path),
    )


@pytest.mark.asyncio
async def test_default_tavily_filters_sorts_deduplicates_before_limit(monkeypatch, tmp_path):
    calls = install(
        monkeypatch,
        {
            "request_id": "req-1",
            "results": [
                item("https://outside.example/x"),
                item("https://nea.gov.cn.evil.org/x"),
                item("https://reuters.com/news"),
                item("https://iea.org/data"),
                item("https://www.nea.gov.cn/solar"),
                item("https://www.nea.gov.cn/solar#copy"),
                item("https://stats.gov.cn/data"),
            ],
        },
    )
    result = await execute(tmp_path, max_results=2)
    assert not result.is_error and result.metadata["search_provider"] == "tavily"
    assert [s["locator"] for s in result.metadata["research_source_specs"]] == [
        "https://www.nea.gov.cn/solar",
        "https://stats.gov.cn/data",
    ]
    assert result.metadata["request_ids"] == ["req-1"]
    assert result.metadata["research_source_specs"][0]["published_at"] == "2026-10-01"
    url, args = calls[0]
    assert url == tavily.SEARCH_URL and args["method"] == "POST" and args["max_redirects"] == 0
    assert args["json"]["query"] == "光伏 装机 累计 同比"
    assert args["json"]["max_results"] == 20 and args["json"]["include_domains_mode"] == "restrict"
    assert args["json"]["auto_parameters"] is False and args["json"]["include_answer"] is False
    assert args["headers"]["Authorization"] == "Bearer test-tavily-secret"


@pytest.mark.asyncio
async def test_macro_domains_and_explicit_web(monkeypatch, tmp_path):
    calls = install(monkeypatch, {"results": [item("https://outside.example/data")]})
    result = await execute(tmp_path, category="macro")
    assert "nea.gov.cn" in calls[0][1]["json"]["include_domains"]
    assert "reuters.com" not in calls[0][1]["json"]["include_domains"]
    assert not result.is_error and result.metadata["outcome"] == "empty"
    assert result.metadata["research_source_specs"] == [] and len(calls) == 1
    result = await execute(tmp_path, scope="web", category="news")
    assert "include_domains" not in calls[1][1]["json"]
    assert calls[1][1]["json"]["topic"] == "news"
    assert result.metadata["source_classifications"][0]["listed"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,code",
    [
        (401, "authentication"),
        (429, "rate_limit"),
        (432, "quota"),
        (433, "quota"),
        (422, "invalid_request"),
        (500, "upstream"),
    ],
)
async def test_http_failure_is_safe_explicit_and_not_retried(monkeypatch, tmp_path, status, code):
    calls = install(monkeypatch, {"detail": "test-tavily-secret upstream echo"}, status)
    result = await execute(tmp_path)
    assert result.is_error and result.metadata["error_codes"] == [code]
    assert len(calls) == 1 and "test-tavily-secret" not in str(result)
    assert result.metadata["research_source_specs"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [{}, {"results": None}, {"results": [{}]}, ["error"]])
async def test_invalid_response_is_not_empty(monkeypatch, tmp_path, data):
    install(monkeypatch, data)
    result = await execute(tmp_path)
    assert result.is_error and result.metadata["error_codes"] == ["invalid_response"]


@pytest.mark.asyncio
async def test_missing_key_and_precedence(monkeypatch, tmp_path):
    calls = install(monkeypatch)
    monkeypatch.delenv("TAVILY_API_KEY")
    result = await execute(tmp_path)
    assert not calls and result.metadata["error_codes"] == ["configuration"]
    monkeypatch.setattr(tavily, "load_credential", lambda *args: "stored")
    assert tavily.resolve_tavily_key() == "stored"
    monkeypatch.setenv("TAVILY_API_KEY", "env")
    assert tavily.resolve_tavily_key() == "env"
    monkeypatch.setenv("RESEARCHX_TAVILY_API_KEY", "prefixed")
    assert tavily.resolve_tavily_key() == "prefixed"
    assert tavily.tavily_credentials() == {"stored", "env", "prefixed"}


@pytest.mark.asyncio
async def test_batch_deadline_keeps_results_and_cancels_requests(monkeypatch):
    monkeypatch.setattr(tavily, "SEARCH_TIMEOUT", 0.04)
    calls, cancelled = [], []
    active = peak = 0

    async def fetch(url, **kwargs):
        nonlocal active, peak
        group = kwargs["json"]["include_domains"]
        calls.append(group)
        active += 1
        peak = max(active, peak)
        try:
            if group[0] == "site0.example":
                return httpx.Response(200, json={"results": [item("https://site0.example/a")]})
            await asyncio.Event().wait()
        finally:
            active -= 1
            cancelled.append(group[0])

    monkeypatch.setattr(tavily, "fetch_public_http_response", fetch)
    results = await tavily.search_tavily(
        "topic", domains=[f"site{i}.example" for i in range(1000)], category="all"
    )
    assert len(results) == 4 and results[0].candidates
    assert all(b.error_code == "timeout" for b in results[1:])
    assert peak == 2 and active == 0 and len(calls) == len(cancelled) == 3
    assert all(len(group) <= 300 for group in calls)


@pytest.mark.asyncio
async def test_custom_html_endpoint_never_receives_tavily_credentials(monkeypatch, tmp_path):
    calls = []

    async def fetch(url, **kwargs):
        calls.append(kwargs)
        return httpx.Response(
            200,
            text='<div class="no-results">No results found</div>',
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr("researchx.tools.web_search_tool.fetch_public_http_response", fetch)
    result = await execute(tmp_path, search_url="https://custom.example/search", scope="web")
    assert not result.is_error and result.metadata["outcome"] == "empty"
    assert "Authorization" not in calls[0]["headers"]


@pytest.mark.asyncio
async def test_html_challenge_is_not_reported_as_empty(monkeypatch, tmp_path):
    async def fetch(url, **kwargs):
        return httpx.Response(200, text="captcha challenge", request=httpx.Request("GET", url))

    monkeypatch.setattr("researchx.tools.web_search_tool.fetch_public_http_response", fetch)
    result = await execute(tmp_path, search_url="https://custom.example/search", scope="web")
    assert result.is_error and result.metadata["error_codes"] == ["invalid_response"]
