from __future__ import annotations

import json
import os
from pathlib import Path

from app.config import Settings
from app.schemas import LLMRuntimeConfig, ProviderOption


class ProviderRegistry:
    def __init__(self, settings: Settings):
        self.settings = settings

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
                base_url=spec.get("base_url", ""),
                model=spec.get("model", ""),
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
        api_key = api_key_override or (os.getenv(api_key_env) if api_key_env else None)
        base_url = base_url_override or spec.get("base_url", "")
        model = model_override or spec.get("model", "")

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

