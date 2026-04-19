from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Grafinder"
    app_host: str = "0.0.0.0"
    app_port: int = 8080

    database_url: str = "postgresql+asyncpg://grafinder:grafinder@localhost:5432/grafinder"

    grafana_api_url: str = "http://localhost:3000"
    grafana_public_url: str = "http://localhost:3000"
    grafana_username: str = "admin"
    grafana_password: str = "grafinder_admin"
    grafana_datasource_uid: str = "grafinder-postgres"

    search_result_limit: int = 8
    max_documents_per_task: int = 5
    crawl_timeout_seconds: int = 45
    crawl_max_markdown_chars: int = 12_000
    network_timeout_seconds: int = 60
    http_proxy: str | None = None
    https_proxy: str | None = None
    all_proxy: str | None = None
    no_proxy: str | None = None

    llm_required: bool = True
    auto_open_browser: bool = False
    llm_providers_file: str = "config/llm_providers.example.json"

    @property
    def llm_providers_path(self) -> Path:
        return Path(self.llm_providers_file)

    @property
    def preferred_proxy(self) -> str | None:
        return self.https_proxy or self.http_proxy or self.all_proxy

    def ddgs_proxies(self) -> dict[str, str] | str | None:
        if self.http_proxy and self.https_proxy:
            return {"http": self.http_proxy, "https": self.https_proxy}
        return self.preferred_proxy

    def apply_process_proxy_env(self) -> None:
        if self.http_proxy:
            os.environ["HTTP_PROXY"] = self.http_proxy
            os.environ["http_proxy"] = self.http_proxy
        if self.https_proxy:
            os.environ["HTTPS_PROXY"] = self.https_proxy
            os.environ["https_proxy"] = self.https_proxy
        if self.all_proxy:
            os.environ["ALL_PROXY"] = self.all_proxy
            os.environ["all_proxy"] = self.all_proxy
        if self.no_proxy:
            os.environ["NO_PROXY"] = self.no_proxy
            os.environ["no_proxy"] = self.no_proxy


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
