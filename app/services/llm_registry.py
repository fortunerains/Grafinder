from __future__ import annotations

import json
import os
from pathlib import Path

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
    def _derived_env_name(api_key_env: str | None, suffix: str) -> str | None:
        if not api_key_env or not api_key_env.endswith("_API_KEY"):
            return None
        prefix = api_key_env[: -len("_API_KEY")]
        return f"{prefix}_{suffix}"

    def _resolve_spec_value(self, spec: dict, field: str) -> str:
        env_field_name = f"{field}_env"
        explicit_env_name = spec.get(env_field_name)
        derived_env_name = None
        if field == "base_url":
            derived_env_name = self._derived_env_name(spec.get("api_key_env"), "BASE_URL")
        elif field == "model":
            derived_env_name = self._derived_env_name(spec.get("api_key_env"), "MODEL")

        env_value = self._read_env(explicit_env_name) or self._read_env(derived_env_name)
        return env_value or spec.get(field, "")

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
                    "model": "gpt-4.1-mini",
                    "api_key_env": "OPENAI_API_KEY",
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
        api_key_env = spec.get("api_key_env")
        api_key = api_key_override or self._read_env(api_key_env)
        base_url = base_url_override or self._resolve_spec_value(spec, "base_url")
        model = model_override or self._resolve_spec_value(spec, "model")

        if self.settings.llm_required and not api_key:
            hint = api_key_env or "your provider API key"
            raise ValueError(f"Missing API key for provider '{selected_name}'. Configure {hint} or fill the advanced field.")

        return LLMRuntimeConfig(
            provider=selected_name,
            label=spec.get("label", selected_name),
            base_url=base_url,
            model=model,
            api_key=api_key,
        )
