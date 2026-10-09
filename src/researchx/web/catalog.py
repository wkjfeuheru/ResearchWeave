"""Profiles, credentials and plugin cards shared with the CLI."""

from __future__ import annotations

from typing import cast
from uuid import uuid4

from fastapi import HTTPException

from researchx.auth.manager import AuthManager
from researchx.config import Settings, load_settings, save_settings
from researchx.config.settings import ProviderProfile, builtin_provider_profile_names
from researchx.plugins import load_plugins
from researchx.web.models import ModelInput


from typing_extensions import TypedDict


class ModelCard(TypedDict):
    id: str
    label: str
    api_format: str
    model: str
    base_url: str | None
    context_window_tokens: int | None
    auto_compact_threshold_tokens: int | None
    configured: bool
    active: bool
    supported: bool
    editable: bool
    auth_source: str
    builtin: bool


class ModelCatalog(TypedDict):
    active_profile: str
    items: list[ModelCard]


def profile_settings(profile_id: str) -> Settings:
    settings = load_settings()
    profile = settings.merged_profiles().get(profile_id)
    if profile is None:
        raise HTTPException(404, "模型配置不存在")
    if profile.api_format not in {"openai", "openai_compat", "anthropic", "copilot"}:
        raise HTTPException(400, "此模型接口格式不受支持")
    return settings.model_copy(update={"active_profile": profile_id}).materialize_active_profile()


def models_list() -> ModelCatalog:
    manager = AuthManager()
    active = manager.get_active_profile()
    builtins = builtin_provider_profile_names()
    items: list[ModelCard] = []
    for name, profile in manager.list_profiles().items():
        supported = profile.api_format in {"openai", "openai_compat", "anthropic", "copilot"}
        editable = profile.auth_source not in {
            "codex_subscription",
            "claude_subscription",
            "copilot_oauth",
        }
        configured = False
        if supported:
            try:
                if profile.auth_source == "copilot_oauth":
                    from researchx.api.copilot_auth import load_copilot_auth

                    configured = load_copilot_auth() is not None
                else:
                    configured = bool(profile_settings(name).resolve_auth().value)
            except (ValueError, HTTPException):
                pass
        items.append(
            {
                "id": name,
                "label": profile.label,
                "api_format": profile.api_format,
                "model": profile.resolved_model,
                "base_url": profile.base_url,
                "context_window_tokens": profile.context_window_tokens,
                "auto_compact_threshold_tokens": profile.auto_compact_threshold_tokens,
                "configured": configured,
                "active": name == active,
                "supported": supported,
                "editable": editable,
                "auth_source": profile.auth_source,
                "builtin": name in builtins,
            }
        )
    return {"active_profile": active, "items": items}


def save_model(data: ModelInput, profile_id: str | None = None) -> str:
    manager = AuthManager()
    old = None
    if profile_id:
        profile_settings(profile_id)
        old = manager.list_profiles()[profile_id]
        if old.auth_source in {"codex_subscription", "claude_subscription", "copilot_oauth"}:
            raise HTTPException(400, "订阅认证配置请使用 CLI 管理；网页可选择并测试连接")
    else:
        profile_id = f"web-{uuid4().hex[:12]}"
    # Preserve an existing slot when editing; new API configs have independent credentials.
    profile = ProviderProfile(
        label=data.label,
        provider=data.api_format,
        api_format=data.api_format,
        auth_source=f"{data.api_format}_api_key",
        default_model=data.model,
        last_model=data.model,
        base_url=data.base_url,
        credential_slot=old.credential_slot if old else profile_id,
        context_window_tokens=(
            data.context_window_tokens
            if "context_window_tokens" in data.model_fields_set
            else old.context_window_tokens
            if old and old.resolved_model == data.model
            else None
        ),
        auto_compact_threshold_tokens=(
            data.auto_compact_threshold_tokens
            if "auto_compact_threshold_tokens" in data.model_fields_set
            else old.auto_compact_threshold_tokens
            if old
            else None
        ),
    )
    manager.upsert_profile(profile_id, profile)
    if data.api_key and data.api_key.get_secret_value().strip():
        manager.store_profile_credential(
            profile_id, "api_key", data.api_key.get_secret_value().strip()
        )
    return profile_id


def skills_list(cwd: str) -> list[dict[str, object]]:
    plugins = load_plugins(load_settings(), cwd, metadata_only=True)
    cards = []
    for plugin in plugins:
        cards.append(
            {
                "id": plugin.name,
                "label": plugin.manifest.display_name or plugin.name,
                "description": plugin.description,
                "version": plugin.manifest.version,
                "enabled": plugin.enabled,
                "diagnostics": plugin.diagnostics,
                "category": plugin.manifest.category,
                "example": plugin.manifest.example,
                "skills": [
                    {
                        "name": s.command_name or s.name,
                        "description": s.description,
                        "enabled": s.enabled,
                        "metadata": s.metadata.model_dump(),
                        "entrypoint": str(s.path),
                        "base_dir": str(s.base_dir),
                    }
                    for s in plugin.skills
                ],
            }
        )
    return cards


def toggle_skill(cwd: str, plugin_id: str, enabled: bool) -> None:
    settings = load_settings()
    cards = skills_list(cwd)
    if any(p["id"] == plugin_id for p in cards):
        updated = {**settings.enabled_plugins, plugin_id: enabled}
        save_settings(settings.model_copy(update={"enabled_plugins": updated}))
    elif any(
        s["name"] == plugin_id for p in cards for s in cast(list[dict[str, object]], p["skills"])
    ):
        # Retain PATCH /skills/<old-business-plugin-id> for old clients/configs.
        selected = next(
            s
            for p in cards
            for s in cast(list[dict[str, object]], p["skills"])
            if s["name"] == plugin_id
        )
        metadata = selected["metadata"]
        key = (
            str(metadata.get("skill_id") or plugin_id) if isinstance(metadata, dict) else plugin_id
        )
        updated = {**settings.enabled_skills, key: enabled}
        save_settings(settings.model_copy(update={"enabled_skills": updated}))
    else:
        raise HTTPException(404, "技能插件不存在")


def skill_detail(cwd: str, plugin_id: str, name: str) -> dict[str, object]:
    """Only a specifically requested entrypoint is read; resources remain unloaded."""
    for plugin in load_plugins(load_settings(), cwd, metadata_only=True):
        if plugin.name != plugin_id:
            continue
        for skill in plugin.skills:
            if name not in {skill.name, skill.command_name}:
                continue
            if not skill.enabled or skill.metadata.status in {"draft", "retired"}:
                raise HTTPException(409, "技能已禁用或未发布")
            try:
                return {"name": name, "content": skill.load_content()}
            except (ValueError, OSError) as exc:
                raise HTTPException(400, str(exc)) from exc
    raise HTTPException(404, "技能不存在")
