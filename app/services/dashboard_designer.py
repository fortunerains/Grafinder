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
            "panels 每项字段为：panel_type、title、description、metric_operation、metric_field、group_by、time_field、time_grain、columns、limit、sort_direction。"
            "只能使用以下枚举值："
            "panel_type=timeseries|barchart|table|stat；"
            "metric_operation=count|sum|avg|max|min；"
            "metric_field=*|metric_value；"
            "group_by=none|entity|source_name|metric_name|title|metric_unit；"
            "time_field=published_at|created_at；"
            "time_grain=day|week|month；"
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
- timeseries 适合时间趋势，barchart 适合排行对比，table 适合明细追溯，stat 适合单一核心数字
- 如果用户要求“重新生成”或“换一种图”，请优先调整 panels 的类型、排序和聚合方式
- title 用中文，面向业务用户
""".strip()

        payload = await self.llm_client.complete_json(runtime, system_prompt, user_prompt)
        design = DashboardDesign.model_validate(payload)
        if not design.panels:
            raise ValueError("LLM did not return any dashboard panels.")
        return design

    def build_default_design(self, keyword: str, intent: str, panel_hint: str) -> DashboardDesign:
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

    @staticmethod
    def _ordered_panel_types(panel_hint: str) -> list[str]:
        if panel_hint == "timeseries":
            return ["timeseries", "barchart", "table", "stat"]
        if panel_hint == "barchart":
            return ["barchart", "timeseries", "table", "stat"]
        if panel_hint == "table":
            return ["table", "timeseries", "barchart", "stat"]
        return ["timeseries", "barchart", "table", "stat"]
