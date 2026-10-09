"""Retirement boundary: legacy image settings never enable a model-callable generator."""

from unittest.mock import AsyncMock
import pytest
from researchx.config.settings import ImageGenerationConfig, PermissionSettings
from researchx.engine.query import QueryContext, _execute_tool_call_impl
from researchx.permissions.checker import PermissionChecker
from researchx.permissions.modes import PermissionMode
from researchx.tools import create_research_tool_registry


@pytest.mark.parametrize("provider", ["openai", "codex", "auto"])
@pytest.mark.parametrize("mode", ["research", "general"])
async def test_generation_is_not_callable_even_with_legacy_credentials(
    tmp_path, monkeypatch, provider, mode
):
    monkeypatch.setenv("RESEARCHX_DATA_DIR", str(tmp_path / "data"))
    client = AsyncMock()
    registry = create_research_tool_registry(mode=mode)
    context = QueryContext(
        api_client=client,
        tool_registry=registry,
        permission_checker=PermissionChecker(PermissionSettings(mode=PermissionMode.FULL_AUTO)),
        cwd=tmp_path,
        model="test",
        max_tokens=100,
        system_prompt="test",
        tool_metadata={
            "image_generation_config": {"api_key": "secret", "codex_auth_token": "token"}
        },
    )
    output = tmp_path / "assets/cat.png"
    result = await _execute_tool_call_impl(
        context,
        "image_generation",
        "retired",
        {"prompt": "a cat", "provider": provider, "output_path": str(output)},
    )
    assert result.is_error and "Unknown tool" in result.content
    assert registry.get("image_generation") is None
    assert "image_generation" not in {item["name"] for item in registry.to_api_schema()}
    assert not output.exists() and not output.parent.exists()
    assert client.mock_calls == []


def test_image_generation_config_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RESEARCHX_IMAGE_GENERATION_PROVIDER", "openai")
    monkeypatch.setenv("RESEARCHX_IMAGE_GENERATION_MODEL", "gpt-image-1")
    monkeypatch.setenv("RESEARCHX_IMAGE_GENERATION_API_KEY", "sk-test")
    monkeypatch.setenv("RESEARCHX_IMAGE_GENERATION_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("RESEARCHX_IMAGE_GENERATION_CODEX_MODEL", "gpt-5.4")

    cfg = ImageGenerationConfig.from_env()

    assert cfg.provider == "openai"
    assert cfg.model == "gpt-image-1"
    assert cfg.api_key == "sk-test"
    assert cfg.base_url == "https://example.test/v1"
    assert cfg.codex_model == "gpt-5.4"
    assert cfg.is_configured
