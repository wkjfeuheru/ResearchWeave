"""Source policy and search/fetch integration without live search dependencies."""

from types import SimpleNamespace
import html

import httpx
import pytest
from pydantic import ValidationError

from researchx.config.settings import (
    ResearchSiteConfig,
    Settings,
    WebSettings,
    load_settings,
    save_settings,
)
from researchx.config import sites as research_sites
from researchx.tools.base import ToolExecutionContext
from researchx.config.sites import classify_source, get_research_sites
from researchx.tools.web_fetch_tool import WebFetchTool, WebFetchToolInput
from researchx.tools.web_search_tool import WebSearchTool, WebSearchToolInput


@pytest.fixture
def catalogue(monkeypatch):
    monkeypatch.setenv("RESEARCHX_WEB_SEARCH_URL", "https://search.example/html")
    settings = WebSettings(
        research_sites=[
            ResearchSiteConfig(
                domain="official.example",
                name="官方",
                categories=["macro", "policy"],
                tier="official",
            ),
            ResearchSiteConfig(
                domain="disclosure.example", categories=["disclosure"], tier="official"
            ),
            ResearchSiteConfig(
                domain="industry.example", categories=["industry"], tier="professional"
            ),
            ResearchSiteConfig(domain="media.example", categories=["news"], tier="media"),
        ]
    )
    monkeypatch.setattr(research_sites, "_BUILTIN_SITES", ())
    monkeypatch.setattr(research_sites, "load_settings", lambda: SimpleNamespace(web=settings))
    return settings


def install_search(monkeypatch, urls):
    calls = []

    async def fetch(url, **kwargs):
        calls.append((url, kwargs["params"]["q"]))
        body = "".join(
            f'<li class="b_algo"><h2><a href="{html.escape(target, quote=True)}">资料 {index}</a></h2>'
            f"<p>原始摘要 {index}</p></li>"
            for index, target in enumerate(urls)
        )
        return httpx.Response(200, text=body, request=httpx.Request("GET", url))

    monkeypatch.setattr("researchx.tools.web_search_tool.fetch_public_http_response", fetch)
    return calls


@pytest.mark.asyncio
async def test_curated_filters_before_limit_deduplicates_and_orders_tiers(
    tmp_path, monkeypatch, catalogue
):
    calls = install_search(
        monkeypatch,
        [
            "https://outside.example/a",
            "https://official.example.evil.org/a",
            "https://fakeofficial.example/a",
            "https://media.example/news",
            "https://industry.example/report",
            "https://sub.official.example/a",
            "https://sub.official.example/a#duplicate",
            "https://official.example/b",
            "https://disclosure.example/c",
        ],
    )
    result = await WebSearchTool().execute(
        WebSearchToolInput(query="经济政策", max_results=4),
        ToolExecutionContext(cwd=tmp_path),
    )
    assert not result.is_error
    urls = [entry["locator"] for entry in result.metadata["research_source_specs"]]
    assert urls == [
        "https://sub.official.example/a",
        "https://official.example/b",
        "https://disclosure.example/c",
        "https://industry.example/report",
    ]
    assert len(calls) == 1
    assert "经济政策" in calls[0][1] and "site:official.example" in calls[0][1]
    assert all(source["listed"] for source in result.metadata["source_classifications"])
    assert all(entry["fragment"] for entry in result.metadata["research_source_specs"])
    assert "目录收录不代表事实已核验" in result.output


@pytest.mark.asyncio
async def test_category_restricts_queries_and_results_without_automatic_expansion(
    tmp_path, monkeypatch, catalogue
):
    calls = install_search(
        monkeypatch, ["https://industry.example/report", "https://official.example/data"]
    )
    result = await WebSearchTool().execute(
        WebSearchToolInput(query="经济增长", category="macro"),
        ToolExecutionContext(cwd=tmp_path),
    )
    assert not result.is_error
    assert len(calls) == 1 and "site:official.example" in calls[0][1]
    assert "site:industry.example" not in calls[0][1]
    assert len(result.metadata["research_source_specs"]) == 1
    assert "请求 5 条，实际找到 1 条" in result.output and "范围没有自动扩大" in result.output


@pytest.mark.asyncio
async def test_web_scope_keeps_topic_and_marks_unlisted_sources(tmp_path, monkeypatch, catalogue):
    calls = install_search(monkeypatch, ["https://outside.example/a", "https://media.example/news"])
    topic = "公司 海外收入 2026"
    result = await WebSearchTool().execute(
        WebSearchToolInput(query=topic, scope="web", category="disclosure"),
        ToolExecutionContext(cwd=tmp_path),
    )
    assert not result.is_error and calls[0][1] == topic
    assert [source["listed"] for source in result.metadata["source_classifications"]] == [
        True,
        False,
    ]
    assert "目录外来源" in result.output


@pytest.mark.asyncio
async def test_zero_curated_results_do_not_query_web(tmp_path, monkeypatch, catalogue):
    calls = install_search(monkeypatch, ["https://outside.example/a"])
    result = await WebSearchTool().execute(
        WebSearchToolInput(query="行业", category="industry"),
        ToolExecutionContext(cwd=tmp_path),
    )
    assert not result.is_error and len(calls) == 1
    assert result.metadata["outcome"] == "empty"
    assert result.metadata["research_source_specs"] == []
    assert "scope=web" in result.output


@pytest.mark.asyncio
async def test_category_with_no_sites_makes_no_request(tmp_path, monkeypatch, catalogue):
    catalogue.research_sites[-1].enabled = False
    calls = install_search(monkeypatch, [])
    result = await WebSearchTool().execute(
        WebSearchToolInput(query="新闻", category="news"),
        ToolExecutionContext(cwd=tmp_path),
    )
    assert not result.is_error and not calls
    assert result.metadata["outcome"] == "empty"


@pytest.mark.asyncio
async def test_search_partial_failure_preserves_other_results(tmp_path, monkeypatch, catalogue):
    monkeypatch.setattr("researchx.tools.web_search_tool.SITES_PER_QUERY", 1)
    calls = []

    async def fetch(url, **kwargs):
        query = kwargs["params"]["q"]
        calls.append(query)
        if "site:official.example" in query:
            raise httpx.ConnectTimeout("")
        return httpx.Response(
            200,
            text='<a class="result__a" href="https://industry.example/a">行业资料</a>',
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr("researchx.tools.web_search_tool.fetch_public_http_response", fetch)
    result = await WebSearchTool().execute(
        WebSearchToolInput(query="产业"), ToolExecutionContext(cwd=tmp_path)
    )
    assert not result.is_error and len(result.metadata["research_source_specs"]) == 1
    assert "ConnectTimeout" in result.output and len(calls) == 4
    assert all("site:" in query for query in calls)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "final_url,listed",
    [("https://sub.official.example/data", True), ("https://outside.example/data", False)],
)
async def test_fetch_classifies_final_url_and_keeps_snapshot(
    tmp_path, monkeypatch, catalogue, final_url, listed
):
    async def fetch(*args, **kwargs):
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<title>资料</title><p>原始正文</p>",
            request=httpx.Request("GET", final_url),
        )

    monkeypatch.setattr("researchx.tools.web_fetch_tool.fetch_public_http_response", fetch)
    result = await WebFetchTool().execute(
        WebFetchToolInput(url="https://media.example/redirect"),
        ToolExecutionContext(cwd=tmp_path),
    )
    assert not result.is_error
    assert result.metadata["source_classification"]["listed"] is listed
    assert result.metadata["source_classification"]["url"] == final_url
    source = result.metadata["research_source_specs"][0]
    assert source["locator"] == final_url and "原始正文" in source["content"]
    assert "目录收录" not in source["content"]


def test_catalogue_partial_overrides_survive_settings_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCHX_CONFIG_DIR", str(tmp_path / "config"))
    settings = Settings(
        web=WebSettings(
            research_sites=[
                ResearchSiteConfig(domain="stats.gov.cn", name="官方统计"),
                ResearchSiteConfig(domain="reuters.com", enabled=False),
                ResearchSiteConfig(
                    domain="ir.company.example",
                    name="公司IR",
                    categories=["disclosure"],
                    tier="official",
                ),
            ]
        )
    )
    save_settings(settings)
    sites = get_research_sites(load_settings().web)
    stats = next(site for site in sites if site.domain == "stats.gov.cn")
    assert stats.name == "官方统计" and stats.categories == ["macro"] and stats.tier == "official"
    assert not any(site.domain == "reuters.com" for site in sites)
    assert classify_source("https://ir.company.example/report", sites)["tier"] == "official"
    assert not classify_source("https://unconfigured-company.example", sites)["listed"]


def test_more_specific_site_classification_wins():
    sites = get_research_sites(
        WebSettings(
            research_sites=[
                ResearchSiteConfig(domain="example.com", categories=["news"], tier="media"),
                ResearchSiteConfig(
                    domain="ir.example.com", categories=["disclosure"], tier="official"
                ),
            ]
        )
    )
    assert classify_source("https://ir.example.com/report", sites)["tier"] == "official"


@pytest.mark.parametrize(
    "domain",
    [
        "https://stats.gov.cn",
        "*.gov.cn",
        "stats.gov.cn:443",
        "stats.gov.cn/a",
        "-bad.example",
        "127.0.0.1",
    ],
)
def test_research_domains_reject_non_hostnames(domain):
    with pytest.raises(ValidationError):
        ResearchSiteConfig(domain=domain)
