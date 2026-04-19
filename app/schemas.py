from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ProviderAdapter = Literal["openai_compatible_chat"]
JsonOutputMode = Literal["auto", "response_format", "prompt_only"]


class ProviderOption(BaseModel):
    name: str
    label: str
    base_url: str
    model: str
    model_options: list[str] = Field(default_factory=list)
    adapter: ProviderAdapter = "openai_compatible_chat"
    json_mode: JsonOutputMode = "auto"
    description: str | None = None


class LLMRuntimeConfig(BaseModel):
    provider: str
    label: str
    base_url: str
    model: str
    api_key: str | None = None
    adapter: ProviderAdapter = "openai_compatible_chat"
    json_mode: JsonOutputMode = "auto"


class TaskCreate(BaseModel):
    keyword: str = Field(min_length=1, max_length=255)
    intent: str = Field(min_length=1, max_length=120)
    source_hint: str | None = Field(default=None, max_length=1000)
    llm_provider: str | None = None
    llm_base_url: str | None = None
    llm_model: str | None = None
    llm_api_key: str | None = None

    @field_validator("keyword", "intent", mode="before")
    @classmethod
    def trim_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("source_hint", "llm_base_url", "llm_model", "llm_api_key", mode="before")
    @classmethod
    def blank_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


class TaskRefine(BaseModel):
    instruction: str = Field(min_length=1, max_length=1000)
    llm_provider: str | None = None
    llm_base_url: str | None = None
    llm_model: str | None = None
    llm_api_key: str | None = None

    @field_validator("instruction", mode="before")
    @classmethod
    def trim_instruction(cls, value: str) -> str:
        return value.strip()

    @field_validator("llm_base_url", "llm_model", "llm_api_key", mode="before")
    @classmethod
    def blank_to_none_for_refine(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


class LLMTestRequest(BaseModel):
    llm_provider: str | None = None
    llm_base_url: str | None = None
    llm_model: str | None = None
    llm_api_key: str | None = None

    @field_validator("llm_base_url", "llm_model", "llm_api_key", mode="before")
    @classmethod
    def blank_to_none_for_test(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


class LLMTestResult(BaseModel):
    ok: bool = True
    provider: str
    model: str
    base_url: str
    latency_ms: int
    message: str


class SearchPlan(BaseModel):
    search_queries: list[str]
    extraction_focus: list[str]
    preferred_panel_type: str
    reasoning: str


class SearchResultItem(BaseModel):
    url: str
    title: str
    snippet: str | None = None
    domain: str | None = None
    rank: int


class CrawledDocument(BaseModel):
    url: str
    title: str
    source_name: str | None = None
    crawl_engine: str | None = None
    published_at: datetime | None = None
    markdown: str
    raw_metadata: dict[str, Any] = Field(default_factory=dict)


class ExtractedItem(BaseModel):
    title: str
    source_url: str
    source_name: str | None = None
    published_at: datetime | None = None
    entity: str | None = None
    metric_name: str | None = None
    metric_value: float | None = None
    metric_unit: str | None = None
    summary: str
    confidence: float | None = None
    raw_payload: dict[str, Any] | None = None


class ExtractionEnvelope(BaseModel):
    document_summary: str
    suggested_panel_type: str
    records: list[ExtractedItem]


class DashboardPanelSpec(BaseModel):
    panel_type: Literal["timeseries", "barchart", "table", "stat"]
    title: str
    description: str | None = None
    metric_operation: Literal["count", "sum", "avg", "max", "min"] = "count"
    metric_field: Literal["*", "metric_value"] = "*"
    group_by: Literal["none", "entity", "source_name", "metric_name", "title", "metric_unit"] = "none"
    time_field: Literal["published_at", "created_at"] = "published_at"
    time_grain: Literal["day", "week", "month"] = "day"
    record_keywords: list[str] = Field(default_factory=list)
    record_metric_names: list[str] = Field(default_factory=list)
    record_entities: list[str] = Field(default_factory=list)
    record_units: list[str] = Field(default_factory=list)
    require_numeric: bool = False
    series_name: str | None = None
    value_mode: Literal["aggregate", "latest"] = "aggregate"
    columns: list[Literal["published_at", "created_at", "title", "entity", "metric_name", "metric_value", "metric_unit", "source_name", "summary", "source_url"]] = Field(default_factory=list)
    limit: int = Field(default=20, ge=1, le=100)
    sort_direction: Literal["asc", "desc"] = "desc"


class DashboardDesign(BaseModel):
    title: str
    description: str | None = None
    panels: list[DashboardPanelSpec]


class TaskRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    keyword: str
    intent: str
    source_hint: str | None
    llm_provider: str
    llm_model: str
    llm_base_url: str
    status: str
    summary: str | None
    error_message: str | None
    dashboard_url: str | None
    dashboard_revision: int
    dashboard_payload: dict[str, Any] | None
    last_refinement_instruction: str | None
    plan_payload: dict[str, Any] | None
    sources_count: int
    documents_count: int
    records_count: int
    created_at: datetime
    updated_at: datetime


class TaskSourceRead(BaseModel):
    rank: int
    title: str
    url: str
    domain: str | None = None
    snippet: str | None = None
    selected: bool = True
    crawl_mode: Literal["page_crawled", "rss_summary_fallback", "discovered_only"]
    document_title: str | None = None
    document_source_name: str | None = None
    document_crawl_engine: str | None = None
    document_published_at: datetime | None = None
    document_summary: str | None = None
