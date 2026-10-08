"""Profiles, credentials and plugin cards shared with the CLI."""

from __future__ import annotations

from uuid import uuid4

from fastapi import HTTPException

from openharness.auth.manager import AuthManager
from openharness.config import load_settings, save_settings
from openharness.config.settings import ProviderProfile, builtin_provider_profile_names
from openharness.plugins import load_plugins
from openharness.web.models import ModelInput



def profile_settings(profile_id: str):
    settings = load_settings()
    profile = settings.merged_profiles().get(profile_id)
    if profile is None:
        raise HTTPException(404, "模型配置不存在")
    if profile.api_format not in {"openai", "openai_compat", "anthropic", "copilot"}:
        raise HTTPException(400, "此模型接口格式不受支持")
    return settings.model_copy(update={"active_profile": profile_id}).materialize_active_profile()


def models_list() -> dict:
    manager = AuthManager()
    active = manager.get_active_profile()
    builtins = builtin_provider_profile_names()
    items = []
    for name, profile in manager.list_profiles().items():
        supported = profile.api_format in {"openai", "openai_compat", "anthropic", "copilot"}
        editable = profile.auth_source not in {"codex_subscription", "claude_subscription", "copilot_oauth"}
        configured = False
        if supported:
            try:
                if profile.auth_source == "copilot_oauth":
                    from openharness.api.copilot_auth import load_copilot_auth
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
        context_window_tokens=(data.context_window_tokens if "context_window_tokens" in data.model_fields_set
            else old.context_window_tokens if old and old.resolved_model == data.model else None),
        auto_compact_threshold_tokens=(data.auto_compact_threshold_tokens
            if "auto_compact_threshold_tokens" in data.model_fields_set
            else old.auto_compact_threshold_tokens if old else None),
    )
    manager.upsert_profile(profile_id, profile)
    if data.api_key and data.api_key.get_secret_value().strip():
        manager.store_profile_credential(
            profile_id, "api_key", data.api_key.get_secret_value().strip()
        )
    return profile_id


def skills_list(cwd: str) -> list[dict]:
    plugins = load_plugins(load_settings(), cwd)
    cards = []
    for plugin in plugins:
        author = plugin.manifest.author or {}
        cards.append(
            {
                "id": plugin.name,
                "label": plugin.manifest.display_name or plugin.name,
                "description": plugin.description,
                "version": plugin.manifest.version,
                "author": author.get("name", "社区贡献者"),
                "enabled": plugin.enabled,
                "diagnostics": plugin.diagnostics,
                                "category": plugin.manifest.category,
                "example": plugin.manifest.example,
                "skills": [
                    {
                        "name": s.command_name or s.name,
                        "description": s.description,
                        "content": s.content,
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
    if not any(p["id"] == plugin_id for p in skills_list(cwd)):
        raise HTTPException(404, "技能插件不存在")
    settings = load_settings()
    updated = {**settings.enabled_plugins, plugin_id: enabled}
    save_settings(settings.model_copy(update={"enabled_plugins": updated}))
