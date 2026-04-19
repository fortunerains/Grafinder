from __future__ import annotations

import json
from typing import Any

from app.schemas import DashboardDesign, DashboardPanelSpec, LLMRuntimeConfig
from app.services.llm import LLMJsonClient


class DashboardDesignerService:
    def __init__(self, llm_client: LLMJsonClient):
        self.llm_client = llm_client

    async def design_dashboard(
        self,
        keyword: str,
        intent: str,
        panel_hint: str,
        dataset_profile: dict[str, Any],
        runtime: LLMRuntimeConfig,
        refinement_instruction: str | None = None,
    ) -> DashboardDesign:
        system_prompt = (
            "你是一个 Grafana Dashboard 设计器。"
            "请根据任务目标、结构化数据概况和用户反馈，输出严格 JSON。"
            "字段为：title、description、panels。"
            "panels 每项字段为：panel_type、title、description、metric_operation、metric_field、group_by、time_field、time_grain、"
            "record_keywords、record_metric_names、record_entities、record_units、require_numeric、series_name、value_mode、columns、limit、sort_direction。"
            "只能使用以下枚举值："
            "panel_type=timeseries|barchart|table|stat；"
            "metric_operation=count|sum|avg|max|min；"
            "metric_field=*|metric_value；"
            "group_by=none|entity|source_name|metric_name|title|metric_unit；"
            "time_field=published_at|created_at；"
            "time_grain=day|week|month；"
            "value_mode=aggregate|latest；"
            "columns=published_at|created_at|title|entity|metric_name|metric_value|metric_unit|source_name|summary|source_url；"
            "sort_direction=asc|desc。"
            "请只设计基于现有数据能查询出来的面板，不要杜撰额外字段。"
        )

        refinement_text = refinement_instruction or "无，按展示意图自动推荐。"
        user_prompt = f"""
关键词: {keyword}
展示意图: {intent}
推荐优先图表: {panel_hint}
用户调整要求: {refinement_text}

数据概况(JSON):
{json.dumps(dataset_profile, ensure_ascii=False, indent=2)}

设计要求:
- 输出 2 到 4 个 panels
- 如果用户明确要求去掉某种图，就不要输出该类型
- 如果数据里 metric_value 很少，就优先 count 聚合
- 如果数据概况里已经识别出可用于价格/数值趋势的 series，优先使用 metric_value 并限制到相关记录
- timeseries 适合时间趋势，barchart 适合排行对比，table 适合明细追溯，stat 适合单一核心数字
- 如果用户要求“重新生成”或“换一种图”，请优先调整 panels 的类型、排序和聚合方式
- title 用中文，面向业务用户
""".strip()

        payload = await self.llm_client.complete_json(runtime, system_prompt, user_prompt)
        design = DashboardDesign.model_validate(payload)
        if not design.panels:
            raise ValueError("LLM did not return any dashboard panels.")
        return design

    def build_default_design(self, keyword: str, intent: str, panel_hint: str, dataset_profile: dict[str, Any]) -> DashboardDesign:
        numeric_first = self._build_numeric_first_design(keyword, intent, dataset_profile)
        if numeric_first is not None:
            return numeric_first

        ordered_types = self._ordered_panel_types(panel_hint)
        panels: list[DashboardPanelSpec] = []
        labels = {
            "timeseries": "时间趋势",
            "barchart": "实体排行",
            "table": "原始明细",
            "stat": "核心统计",
        }

        for panel_type in ordered_types[:3]:
            if panel_type == "timeseries":
                panels.append(
                    DashboardPanelSpec(
                        panel_type="timeseries",
                        title=labels[panel_type],
                        metric_operation="count",
                        metric_field="*",
                        group_by="none",
                        time_field="published_at",
                        time_grain="day",
                    )
                )
            elif panel_type == "barchart":
                panels.append(
                    DashboardPanelSpec(
                        panel_type="barchart",
                        title=labels[panel_type],
                        metric_operation="count",
                        metric_field="*",
                        group_by="entity",
                        limit=10,
                        sort_direction="desc",
                    )
                )
            elif panel_type == "table":
                panels.append(
                    DashboardPanelSpec(
                        panel_type="table",
                        title=labels[panel_type],
                        columns=["published_at", "title", "entity", "metric_name", "metric_value", "metric_unit", "source_name", "summary"],
                        limit=100,
                        sort_direction="desc",
                    )
                )
            else:
                panels.append(
                    DashboardPanelSpec(
                        panel_type="stat",
                        title=labels[panel_type],
                        metric_operation="count",
                        metric_field="*",
                        group_by="none",
                    )
                )

        return DashboardDesign(
            title=f"{keyword} | {intent}",
            description="Grafinder 自动生成的默认仪表盘配置。",
            panels=panels,
        )

    def apply_dataset_defaults(
        self,
        keyword: str,
        intent: str,
        design: DashboardDesign,
        dataset_profile: dict[str, Any],
        force_numeric_first: bool = False,
    ) -> DashboardDesign:
        numeric_first = self._build_numeric_first_design(keyword, intent, dataset_profile)
        if numeric_first is None:
            return design

        if force_numeric_first:
            return DashboardDesign(
                title=design.title or numeric_first.title,
                description=design.description or numeric_first.description,
                panels=numeric_first.panels,
            )

        if any(panel.metric_field == "metric_value" for panel in design.panels):
            return design

        merged_panels = list(numeric_first.panels)
        for panel in design.panels:
            if panel.panel_type == "table":
                continue
            merged_panels.append(panel)
            if len(merged_panels) >= 4:
                break

        return DashboardDesign(
            title=design.title,
            description=design.description or numeric_first.description,
            panels=merged_panels[:4],
        )

    @staticmethod
    def _ordered_panel_types(panel_hint: str) -> list[str]:
        if panel_hint == "timeseries":
            return ["timeseries", "barchart", "table", "stat"]
        if panel_hint == "barchart":
            return ["barchart", "timeseries", "table", "stat"]
        if panel_hint == "table":
            return ["table", "timeseries", "barchart", "stat"]
        return ["timeseries", "barchart", "table", "stat"]

    @staticmethod
    def _build_numeric_first_design(keyword: str, intent: str, dataset_profile: dict[str, Any]) -> DashboardDesign | None:
        preferred_series = dataset_profile.get("preferred_numeric_series") or {}
        current_comparison = dataset_profile.get("current_numeric_comparison") or {}
        current_snapshot = dataset_profile.get("current_numeric_snapshot") or {}
        data_quality_notes = dataset_profile.get("data_quality_notes") or []

        if not preferred_series and not current_snapshot and not current_comparison:
            return None

        focus_keywords = DashboardDesignerService._merged_terms(
            preferred_series.get("keywords"),
            current_comparison.get("keywords"),
            current_snapshot.get("keywords"),
            [keyword],
        )
        focus_metric_names = DashboardDesignerService._merged_terms(
            preferred_series.get("metric_names"),
            current_comparison.get("metric_names"),
            current_snapshot.get("metric_names"),
        )
        focus_entities = DashboardDesignerService._merged_terms(
            preferred_series.get("entities"),
            current_comparison.get("entities"),
            current_snapshot.get("entities"),
        )
        focus_units = DashboardDesignerService._merged_terms(
            preferred_series.get("units"),
            current_comparison.get("units"),
            current_snapshot.get("units"),
        )

        panels: list[DashboardPanelSpec] = []

        if preferred_series:
            panels.append(
                DashboardPanelSpec(
                    panel_type="timeseries",
                    title=preferred_series.get("panel_title") or "相关数值变化趋势",
                    description=preferred_series.get("description") or "按时间展示与当前关键词最相关的数值变化。",
                    metric_operation=preferred_series.get("metric_operation", "avg"),
                    metric_field="metric_value",
                    group_by=preferred_series.get("group_by", "none"),
                    time_field="published_at",
                    time_grain=preferred_series.get("time_grain", "day"),
                    require_numeric=True,
                    record_keywords=preferred_series.get("keywords", focus_keywords),
                    record_metric_names=preferred_series.get("metric_names", []),
                    record_entities=preferred_series.get("entities", []),
                    record_units=preferred_series.get("units", []),
                    series_name=preferred_series.get("series_name") or "数值趋势",
                    sort_direction="asc",
                )
            )

        if current_comparison:
            panels.append(
                DashboardPanelSpec(
                    panel_type="barchart",
                    title=current_comparison.get("panel_title") or "当前重点品类价格对比",
                    description=current_comparison.get("description") or "对比当前抓取到的重点品类价格水平。",
                    metric_operation="avg",
                    metric_field="metric_value",
                    group_by="entity",
                    time_field="created_at",
                    time_grain="day",
                    require_numeric=True,
                    record_keywords=current_comparison.get("keywords", []),
                    record_metric_names=current_comparison.get("metric_names", []),
                    record_entities=current_comparison.get("entities", []),
                    record_units=current_comparison.get("units", []),
                    limit=int(current_comparison.get("limit", 10) or 10),
                    sort_direction="desc",
                )
            )

        if current_snapshot:
            panels.append(
                DashboardPanelSpec(
                    panel_type="stat",
                    title=current_snapshot.get("panel_title") or "当前相关数值",
                    description=current_snapshot.get("description") or "展示当前任务里最新一条可直接用于分析的数值记录。",
                    metric_operation="max",
                    metric_field="metric_value",
                    require_numeric=True,
                    record_keywords=current_snapshot.get("keywords", focus_keywords),
                    record_metric_names=current_snapshot.get("metric_names", []),
                    record_entities=current_snapshot.get("entities", []),
                    record_units=current_snapshot.get("units", []),
                    value_mode="latest",
                )
            )

        panels.append(
            DashboardPanelSpec(
                panel_type="table",
                title="价格记录明细追溯",
                description="优先展示与关键词最相关且可用于分析的数值记录，便于核查来源、时间与单位。",
                metric_operation="count",
                metric_field="*",
                require_numeric=bool(preferred_series or current_snapshot or current_comparison),
                record_keywords=focus_keywords,
                record_metric_names=focus_metric_names,
                record_entities=focus_entities,
                record_units=focus_units,
                columns=["published_at", "title", "entity", "metric_name", "metric_value", "metric_unit", "source_name", "summary", "source_url"],
                limit=40,
                sort_direction="desc",
            )
        )

        description_parts = [f"围绕“{keyword}”自动生成的数值优先看板。"]
        if data_quality_notes:
            description_parts.append("；".join(data_quality_notes[:2]))
        return DashboardDesign(
            title=f"{keyword} | {intent}",
            description=" ".join(description_parts),
            panels=panels[:4],
        )

    @staticmethod
    def _merged_terms(*groups: list[str] | None) -> list[str]:
        seen: set[str] = set()
        merged: list[str] = []
        for group in groups:
            for value in group or []:
                normalized = value.strip()
                if not normalized or normalized in seen:
                    continue
                seen.add(normalized)
                merged.append(normalized)
        return merged[:8]
