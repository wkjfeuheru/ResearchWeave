"""Tavily transport and credentials; source policy stays with web_search."""

from __future__ import annotations

from openharness.utils.async_timeout import timeout as async_timeout
import asyncio
import os
from openharness.utils.search_types import SearchBatch

import httpx

from openharness.auth.storage import load_credential
from openharness.utils.network_guard import NetworkGuardError, fetch_public_http_response

SEARCH_URL = "https://api.tavily.com/search"
SEARCH_TIMEOUT = 30.0
DOMAINS_PER_REQUEST = 300


def tavily_credentials() -> set[str]:
    """Collect all configured keys for redaction, including overridden credentials."""
    return {
        key
        for key in (
            os.environ.get("OPENHARNESS_TAVILY_API_KEY", "").strip(),
            os.environ.get("TAVILY_API_KEY", "").strip(),
            load_credential("tavily", "api_key") or "",
        )
        if key
    }


def resolve_tavily_key() -> str:
    return (
        os.environ.get("OPENHARNESS_TAVILY_API_KEY", "").strip()
        or os.environ.get("TAVILY_API_KEY", "").strip()
        or load_credential("tavily", "api_key")
        or ""
    )


_HTTP_ERRORS = {
    401: ("authentication", "Tavily 密钥无效，请重新配置凭据"),
    403: ("authentication", "Tavily 拒绝访问，请检查账户权限"),
    429: ("rate_limit", "Tavily 请求限流，请稍后再试"),
    432: ("quota", "Tavily 套餐额度已达上限"),
    433: ("quota", "Tavily 按量付费额度已达上限"),
    400: ("invalid_request", "Tavily 请求参数无效"),
    422: ("invalid_request", "Tavily 请求参数校验失败"),
}


async def search_tavily(
    query: str, *, domains: list[str] | None, category: str
) -> list[SearchBatch]:
    """Search bounded domain batches, preserving completed batches on deadline.

    No retry: requests may consume credits even when the response times out.
    ``None`` means explicit web scope; an empty list never widens scope.
    """
    if domains == []:
        return [SearchBatch()]
    key = resolve_tavily_key()
    if not key:
        return [
            SearchBatch(
                error_code="configuration",
                error=(
                    "未配置 Tavily 密钥：运行 oh auth login tavily，或设置 "
                    "OPENHARNESS_TAVILY_API_KEY / TAVILY_API_KEY"
                ),
            )
        ]
    groups = (
        [None]
        if domains is None
        else [
            domains[i : i + DOMAINS_PER_REQUEST]
            for i in range(0, len(domains), DOMAINS_PER_REQUEST)
        ]
    )
    batches = [
        SearchBatch(error_code="timeout", error="Tavily 搜索超过整体时限（30秒）") for _ in groups
    ]
    semaphore = asyncio.Semaphore(2)

    async def fetch(index: int, group: list[str] | None) -> None:
        async with semaphore:
            body = {
                "query": query,
                "max_results": 20,
                "search_depth": "basic",
                "topic": "news" if category == "news" else "general",
                "auto_parameters": False,
                "include_answer": False,
                "include_raw_content": False,
                "include_images": False,
                "include_published_date": True,
            }
            if group is not None:
                body.update(include_domains=group, include_domains_mode="restrict")
            try:
                response = await fetch_public_http_response(
                    SEARCH_URL,
                    method="POST",
                    json=body,
                    headers={"Authorization": f"Bearer {key}"},
                    timeout=SEARCH_TIMEOUT,
                    max_redirects=0,
                    max_bytes=2 * 1024 * 1024,
                )
                if not response.is_success:
                    code, message = _HTTP_ERRORS.get(
                        response.status_code,
                        ("upstream", f"Tavily 服务返回 HTTP {response.status_code}"),
                    )
                    batches[index] = SearchBatch(error_code=code, error=message)
                    return
                data = response.json()
                if not isinstance(data, dict) or not isinstance(data.get("results"), list):
                    raise ValueError("invalid search response")
                candidates = []
                for item in data["results"]:
                    if not isinstance(item, dict) or not all(
                        isinstance(item.get(k), str) for k in ("url", "title", "content")
                    ):
                        raise ValueError("invalid search result")
                    candidates.append(
                        {
                            "url": item["url"],
                            "title": item["title"],
                            "snippet": item["content"],
                            "published_at": item.get("published_date")
                            if isinstance(item.get("published_date"), str)
                            else "",
                        }
                    )
                batches[index] = SearchBatch(
                    candidates=candidates,
                    request_id=data.get("request_id")
                    if isinstance(data.get("request_id"), str)
                    else None,
                )
            except (httpx.TimeoutException, TimeoutError):
                batches[index] = SearchBatch(error_code="timeout", error="Tavily 网络请求超时")
            except NetworkGuardError:
                batches[index] = SearchBatch(
                    error_code="network_policy",
                    error="Tavily 请求被网络防护拒绝（地址、重定向或响应大小）",
                )
            except httpx.HTTPError:
                batches[index] = SearchBatch(
                    error_code="network", error="无法连接 Tavily，请检查网络和代理配置"
                )
            except (ValueError, UnicodeError):
                batches[index] = SearchBatch(
                    error_code="invalid_response", error="Tavily 返回的搜索数据结构无效"
                )

    try:
        async with async_timeout(SEARCH_TIMEOUT):
            await asyncio.gather(*(fetch(i, group) for i, group in enumerate(groups)))
    except asyncio.TimeoutError:
        pass
    return batches
