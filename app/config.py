from __future__ import annotations

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

    llm_required: bool = True
    auto_open_browser: bool = False
    llm_providers_file: str = "config/llm_providers.example.json"

    @property
    def llm_providers_path(self) -> Path:
        return Path(self.llm_providers_file)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

