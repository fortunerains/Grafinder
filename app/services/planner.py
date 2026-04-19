from __future__ import annotations

import re

from app.schemas import LLMRuntimeConfig, SearchPlan
from app.services.llm import LLMJsonClient


class PlannerService:
    def __init__(self, llm_client: LLMJsonClient):
        self.llm_client = llm_client

    async def create_plan(
        self,
        keyword: str,
        intent: str,
        runtime: LLMRuntimeConfig,
        source_hint: str | None = None,
    ) -> SearchPlan:
        system_prompt = (
            "你是一个面向数据发现任务的研究规划器。"
            "请根据用户输入输出严格 JSON，字段为："
            "search_queries(字符串数组)、extraction_focus(字符串数组)、preferred_panel_type、reasoning。"
            "preferred_panel_type 只能是 timeseries、barchart、table、mixed 之一。"
        )
        user_prompt = f"""
用户关键词: {keyword}
展示意图: {intent}
来源偏好/抓取提示: {source_hint or "无，按系统自动发现。"}

请生成:
1. 3 到 5 条适合网页搜索的查询语句
2. 3 到 6 条抽取重点
3. 一个优先图表类型
4. 一句简短的规划理由

要求:
- 搜索语句要覆盖新闻、报告、数据和行业解读
- 如果展示意图含有"趋势"，优先 timeseries
- 如果展示意图含有"排行"，优先 barchart
- 如果展示意图含有"原始"，优先 table
- 如果是自动推荐，优先 mixed
""".strip()

        payload = await self.llm_client.complete_json(runtime, system_prompt, user_prompt)
        plan = SearchPlan.model_validate(payload)
        return SearchPlan(
            search_queries=self._enrich_search_queries(keyword, intent, plan.search_queries, source_hint),
            extraction_focus=self._enrich_extraction_focus(keyword, intent, plan.extraction_focus),
            preferred_panel_type=plan.preferred_panel_type,
            reasoning=plan.reasoning,
        )

    @staticmethod
    def _enrich_search_queries(keyword: str, intent: str, queries: list[str], source_hint: str | None = None) -> list[str]:
        merged = list(queries)
        lower_text = f"{keyword} {intent}"
        if "价" in lower_text or "价格" in lower_text:
            seed = PlannerService._entity_search_seed(keyword)
            direct_first = [
                f"{seed} 价格指数 月度 数据",
                f"{seed} 历史价格 走势图",
                f"{seed} 价格表 行情 数据",
                f"site:mysteel.com {seed} 价格",
                f"site:eastmoney.com {seed} 价格",
            ]
            if "锂" in seed or "电池" in seed:
                direct_first.extend(
                    [
                        "碳酸锂 价格 月度 数据",
                        "磷酸铁锂 电芯 价格 月度 数据",
                    ]
                )

            merged = direct_first + merged
            merged.extend(
                [
                    f"{keyword} 今日价格",
                    f"{keyword} 实时价格 走势图",
                    f"{keyword} 历史价格 数据",
                    f"{keyword} 价格表 行情",
                ]
            )

        if source_hint:
            merged = PlannerService._source_hint_queries(keyword, source_hint) + merged

        seen: set[str] = set()
        deduped: list[str] = []
        for query in merged:
            normalized = query.strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            deduped.append(normalized)
        return deduped[:8]

    @staticmethod
    def _enrich_extraction_focus(keyword: str, intent: str, focuses: list[str]) -> list[str]:
        merged = list(focuses)
        lower_text = f"{keyword} {intent}"
        if "价" in lower_text or "价格" in lower_text:
            merged.extend(
                [
                    "优先抽取最新价、最低价、最高价、中间价、均价等直接价格字段",
                    "优先抽取可形成时间序列的价格点，不要只抽新闻摘要",
                    "如果页面包含价格表、走势图、行情列表，请逐条抽取数值和日期",
                ]
            )

        seen: set[str] = set()
        deduped: list[str] = []
        for item in merged:
            normalized = item.strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            deduped.append(normalized)
        return deduped[:8]

    @staticmethod
    def _entity_search_seed(keyword: str) -> str:
        seed = keyword
        for marker in ["国内价格", "国际价格", "价格走势", "历史价格", "实时价格", "价格", "行情", "走势", "趋势", "数据", "分析", "报告", "国内", "中国"]:
            seed = seed.replace(marker, " ")
        normalized = re.sub(r"\s+", " ", seed).strip()
        return normalized or keyword.strip()

    @staticmethod
    def _source_hint_queries(keyword: str, source_hint: str) -> list[str]:
        seed = PlannerService._entity_search_seed(keyword)
        lowered = source_hint.lower()
        queries: list[str] = []
        for token in PlannerService._hint_domains(source_hint):
            queries.append(f"site:{token} {seed} 价格")
            queries.append(f"site:{token} {seed} 数据")

        name_mappings = {
            "mysteel": "mysteel.com",
            "钢联": "mysteel.com",
            "东方财富": "eastmoney.com",
            "eastmoney": "eastmoney.com",
            "工信部": "miit.gov.cn",
            "miit": "miit.gov.cn",
            "上海有色": "smm.cn",
            "smm": "smm.cn",
            "前瞻": "qianzhan.com",
            "qianzhan": "qianzhan.com",
        }
        for marker, domain in name_mappings.items():
            if marker.lower() in lowered:
                queries.append(f"site:{domain} {seed} 价格")
                queries.append(f"site:{domain} {seed} 数据")

        if "不要新闻" in source_hint or "不看新闻" in source_hint:
            queries.append(f"{seed} 价格 数据 行业站")
            queries.append(f"{seed} 价格 统计 研究")
        return queries

    @staticmethod
    def _hint_domains(source_hint: str) -> list[str]:
        domains = re.findall(r"(?:https?://)?([a-zA-Z0-9.-]+\.[a-zA-Z]{2,})", source_hint)
        seen: set[str] = set()
        items: list[str] = []
        for domain in domains:
            normalized = domain.lower().strip(".")
            if normalized in seen:
                continue
            seen.add(normalized)
            items.append(normalized)
        return items[:6]
