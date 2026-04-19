from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from app.config import Settings
from app.schemas import LLMRuntimeConfig, ProviderOption


class ProviderRegistry:
    def __init__(self, settings: Settings):
        self.settings = settings

    @staticmethod
    def _read_env(name: str | None) -> str | None:
        if not name:
            return None
        value = os.getenv(name)
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @staticmethod
    def _iter_env_names(raw: Any) -> list[str]:
        if raw is None:
            return []
        if isinstance(raw, str):
            return [raw]
        if isinstance(raw, list):
            return [item for item in raw if isinstance(item, str)]
        return []

    def _read_first_env(self, raw: Any) -> str | None:
        for name in self._iter_env_names(raw):
            value = self._read_env(name)
            if value:
                return value
        return None

    @classmethod
    def _derived_env_names(cls, api_key_envs: Any, suffix: str) -> list[str]:
        derived: list[str] = []
        for api_key_env in cls._iter_env_names(api_key_envs):
            if api_key_env.endswith("_API_KEY"):
                prefix = api_key_env[: -len("_API_KEY")]
                derived.append(f"{prefix}_{suffix}")
        return derived

    def _resolve_spec_value(self, spec: dict[str, Any], field: str) -> str:
        env_names: list[str] = []
        env_names.extend(self._iter_env_names(spec.get(f"{field}_env")))
        env_names.extend(self._iter_env_names(spec.get(f"{field}_envs")))
        if field == "base_url":
            env_names.extend(self._derived_env_names(spec.get("api_key_envs") or spec.get("api_key_env"), "BASE_URL"))
        elif field == "model":
            env_names.extend(self._derived_env_names(spec.get("api_key_envs") or spec.get("api_key_env"), "MODEL"))

        env_value = self._read_first_env(env_names)
        return env_value or spec.get(field, "")

    def _resolve_api_key(self, spec: dict[str, Any], override: str | None = None) -> tuple[str | None, list[str]]:
        env_names: list[str] = []
        env_names.extend(self._iter_env_names(spec.get("api_key_env")))
        env_names.extend(self._iter_env_names(spec.get("api_key_envs")))
        return override or self._read_first_env(env_names), env_names

    def _load_registry(self) -> dict:
        path = self.settings.llm_providers_path
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))

        return {
            "default_provider": "openai",
            "providers": {
                "openai": {
                    "label": "OpenAI",
                    "base_url": "https://api.openai.com/v1",
                    "model": "gpt-5.4-mini",
                    "api_key_env": "OPENAI_API_KEY",
                    "adapter": "openai_compatible_chat",
                    "json_mode": "auto",
                }
            },
        }

    def list_providers(self) -> tuple[str, list[ProviderOption]]:
        registry = self._load_registry()
        default_provider = registry.get("default_provider", "openai")
        providers = [
            ProviderOption(
                name=name,
                label=spec.get("label", name),
                base_url=self._resolve_spec_value(spec, "base_url"),
                model=self._resolve_spec_value(spec, "model"),
                model_options=spec.get("model_options", []),
                adapter=spec.get("adapter", "openai_compatible_chat"),
                json_mode=spec.get("json_mode", "auto"),
                description=spec.get("description"),
            )
            for name, spec in registry.get("providers", {}).items()
        ]
        return default_provider, providers

    def resolve(
        self,
        provider_name: str | None = None,
        base_url_override: str | None = None,
        model_override: str | None = None,
        api_key_override: str | None = None,
    ) -> LLMRuntimeConfig:
        registry = self._load_registry()
        default_provider = registry.get("default_provider", "openai")
        providers = registry.get("providers", {})
        selected_name = provider_name or default_provider

        if selected_name not in providers:
            available = ", ".join(sorted(providers.keys()))
            raise ValueError(f"Unknown LLM provider '{selected_name}'. Available providers: {available}")

        spec = providers[selected_name]
        api_key, api_key_env_names = self._resolve_api_key(spec, api_key_override)
        base_url = base_url_override or self._resolve_spec_value(spec, "base_url")
        model = model_override or self._resolve_spec_value(spec, "model")

        if self.settings.llm_required and not api_key:
            hint = " / ".join(api_key_env_names) or "your provider API key"
            raise ValueError(f"Missing API key for provider '{selected_name}'. Configure {hint} or fill the advanced field.")
        if not base_url:
            raise ValueError(f"Missing base URL for provider '{selected_name}'. Configure it in .env, provider config, or the advanced field.")
        if not model:
            raise ValueError(f"Missing model for provider '{selected_name}'. Configure it in .env, provider config, or the advanced field.")

        return LLMRuntimeConfig(
            provider=selected_name,
            label=spec.get("label", selected_name),
            base_url=base_url,
            model=model,
            api_key=api_key,
            adapter=spec.get("adapter", "openai_compatible_chat"),
            json_mode=spec.get("json_mode", "auto"),
        )
