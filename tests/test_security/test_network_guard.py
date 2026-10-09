"""Tests for outbound HTTP target validation."""

from __future__ import annotations

import ipaddress

import httpx
import pytest

from researchx.security.network_guard import fetch_public_http_response


class FakeAsyncClient:
    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def get(self, url: str, **kwargs: object) -> httpx.Response:
        request = httpx.Request("GET", url, params=kwargs.get("params"))
        return httpx.Response(200, text="ok", request=request)


@pytest.fixture(autouse=True)
def isolated_config_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCHX_CONFIG_DIR", str(tmp_path / "config"))


@pytest.mark.asyncio
async def test_fetch_public_http_response_direct_rejects_non_public_dns(monkeypatch):
    async def fake_resolve(host: str, port: int):
        return {ipaddress.ip_address("100.64.1.2")}

    monkeypatch.setattr("researchx.security.network_guard._resolve_host_addresses", fake_resolve)

    with pytest.raises(ValueError, match="synthetic DNS"):
        await fetch_public_http_response("https://example.com/")


@pytest.mark.asyncio
async def test_fetch_public_http_response_synthetic_dns_allows_declared_cidr(monkeypatch):
    async def fake_resolve(host: str, port: int):
        return {ipaddress.ip_address("100.64.1.2")}

    monkeypatch.setenv("RESEARCHX_WEB_RESOLUTION_MODE", "synthetic_dns")
    monkeypatch.setenv("RESEARCHX_WEB_SYNTHETIC_DNS_CIDRS", "100.64.0.0/10")
    monkeypatch.setattr("researchx.security.network_guard._resolve_host_addresses", fake_resolve)
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    response = await fetch_public_http_response("https://example.com/")

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_fetch_public_http_response_synthetic_dns_uses_persisted_settings(
    monkeypatch,
):
    from researchx.config.settings import Settings, WebSettings, save_settings

    async def fake_resolve(host: str, port: int):
        return {ipaddress.ip_address("100.64.1.2")}

    save_settings(
        Settings(
            web=WebSettings(
                resolution_mode="synthetic_dns",
                synthetic_dns_cidrs=["100.64.0.0/10"],
            )
        )
    )
    monkeypatch.setattr("researchx.security.network_guard._resolve_host_addresses", fake_resolve)
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    response = await fetch_public_http_response("https://example.com/")

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_fetch_public_http_response_synthetic_dns_requires_declared_cidrs(monkeypatch):
    monkeypatch.setenv("RESEARCHX_WEB_RESOLUTION_MODE", "synthetic_dns")

    with pytest.raises(ValueError, match="web.synthetic_dns_cidrs"):
        await fetch_public_http_response("https://example.com/")


@pytest.mark.asyncio
async def test_fetch_public_http_response_synthetic_dns_rejects_literal_non_public_ip(
    monkeypatch,
):
    monkeypatch.setenv("RESEARCHX_WEB_RESOLUTION_MODE", "synthetic_dns")
    monkeypatch.setenv("RESEARCHX_WEB_SYNTHETIC_DNS_CIDRS", "100.64.0.0/10")

    with pytest.raises(ValueError, match="non-public"):
        await fetch_public_http_response("http://100.64.1.2/")


@pytest.mark.asyncio
async def test_fetch_public_http_response_synthetic_dns_rejects_undeclared_private_dns(
    monkeypatch,
):
    async def fake_resolve(host: str, port: int):
        return {ipaddress.ip_address("10.0.0.1")}

    monkeypatch.setenv("RESEARCHX_WEB_RESOLUTION_MODE", "synthetic_dns")
    monkeypatch.setenv("RESEARCHX_WEB_SYNTHETIC_DNS_CIDRS", "100.64.0.0/10")
    monkeypatch.setattr("researchx.security.network_guard._resolve_host_addresses", fake_resolve)

    with pytest.raises(ValueError, match="non-public"):
        await fetch_public_http_response("https://example.com/")


@pytest.mark.asyncio
async def test_fetch_public_http_response_proxy_mode_does_not_resolve_target_dns(monkeypatch):
    async def fail_resolve(host: str, port: int):
        raise AssertionError("proxy mode should not resolve ordinary target domains locally")

    monkeypatch.setenv("RESEARCHX_WEB_PROXY", "http://proxy.example.com:7890")
    monkeypatch.setenv("RESEARCHX_WEB_SYNTHETIC_DNS_CIDRS", "not-a-cidr")
    monkeypatch.setattr("researchx.security.network_guard._resolve_host_addresses", fail_resolve)
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    response = await fetch_public_http_response("https://example.com/")

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_fetch_public_http_response_proxy_mode_rejects_literal_non_public_ip(monkeypatch):
    monkeypatch.setenv("RESEARCHX_WEB_PROXY", "http://proxy.example.com:7890")

    with pytest.raises(ValueError, match="non-public"):
        await fetch_public_http_response("http://127.0.0.1/")


@pytest.mark.asyncio
async def test_fetch_public_http_response_rejects_non_public_redirect_in_proxy_mode(
    monkeypatch,
):
    class RedirectClient(FakeAsyncClient):
        async def get(self, url: str, **kwargs: object) -> httpx.Response:
            request = httpx.Request("GET", url, params=kwargs.get("params"))
            return httpx.Response(302, headers={"Location": "http://127.0.0.1/"}, request=request)

    monkeypatch.setenv("RESEARCHX_WEB_PROXY", "http://proxy.example.com:7890")
    monkeypatch.setattr(httpx, "AsyncClient", RedirectClient)

    with pytest.raises(ValueError, match="non-public"):
        await fetch_public_http_response("https://example.com/")


@pytest.mark.asyncio
async def test_bounded_download_decodes_once_and_checks_redirect_targets(monkeypatch):
    import gzip

    transport = httpx.MockTransport(
        lambda req: (
            httpx.Response(302, headers={"location": "https://example.com/final"}, request=req)
            if req.url.path == "/start"
            else httpx.Response(
                200,
                headers={"content-encoding": "gzip"},
                content=gzip.compress(b"%PDF-test-content"),
                request=req,
            )
        )
    )
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: real_client(transport=transport, **kwargs)
    )

    async def allowed(host, port):
        return {ipaddress.ip_address("8.8.8.8")}

    monkeypatch.setattr("researchx.security.network_guard._resolve_host_addresses", allowed)
    response = await fetch_public_http_response("https://example.com/start", max_bytes=100)
    assert response.content == b"%PDF-test-content" and str(response.url).endswith("/final")
    with pytest.raises(ValueError, match="exceeds"):
        await fetch_public_http_response("https://example.com/final", max_bytes=5)


@pytest.mark.asyncio
async def test_bounded_download_rejects_private_redirect_before_fetch(monkeypatch):
    seen = []

    def respond(request):
        seen.append(str(request.url))
        return httpx.Response(
            302, headers={"location": "http://127.0.0.1/private.pdf"}, request=request
        )

    transport = httpx.MockTransport(respond)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: real_client(transport=transport, **kwargs)
    )

    async def allowed(host, port):
        return {ipaddress.ip_address("8.8.8.8")}

    monkeypatch.setattr("researchx.security.network_guard._resolve_host_addresses", allowed)
    with pytest.raises(ValueError, match="non-public"):
        await fetch_public_http_response("https://example.com/start", max_bytes=100)
    assert len(seen) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,body,limit,expected",
    [
        (302, "redirect", 1024, "too many redirects"),
        (200, "x" * 100, 20, "response exceeds"),
        (200, '{"results": []}', 1024, None),
    ],
)
async def test_protected_post_rejects_redirects_and_limits_body(
    monkeypatch, status, body, limit, expected
):
    calls = []
    original = httpx.AsyncClient

    async def handler(request):
        calls.append(request)
        return httpx.Response(status, text=body, headers={"location": "https://elsewhere.example/"})

    def client(**kwargs):
        return original(**kwargs, transport=httpx.MockTransport(handler))

    async def resolve(*args):
        return {ipaddress.ip_address("8.8.8.8")}

    monkeypatch.setattr(httpx, "AsyncClient", client)
    monkeypatch.setattr("researchx.security.network_guard._resolve_host_addresses", resolve)
    request = fetch_public_http_response(
        "https://api.tavily.com/search",
        method="POST",
        json={"query": "光伏"},
        headers={"Authorization": "Bearer test-key"},
        max_redirects=0,
        max_bytes=limit,
    )
    if expected:
        with pytest.raises(ValueError, match=expected):
            await request
    else:
        assert (await request).json() == {"results": []}
    assert len(calls) == 1
    assert calls[0].method == "POST" and calls[0].headers["authorization"] == "Bearer test-key"
    assert calls[0].url.host == "api.tavily.com"


@pytest.mark.asyncio
async def test_post_cannot_enable_redirects_or_access_private_hosts():
    with pytest.raises(ValueError, match="disable redirects"):
        await fetch_public_http_response("https://api.tavily.com/search", method="POST")
    with pytest.raises(ValueError, match="non-public"):
        await fetch_public_http_response("http://127.0.0.1/search", method="POST", max_redirects=0)
