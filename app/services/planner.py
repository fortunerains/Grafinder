from __future__ import annotations

from app.schemas import LLMRuntimeConfig, SearchPlan
from app.services.llm import LLMJsonClient


class PlannerService:
    def __init__(self, llm_client: LLMJsonClient):
        self.llm_client = llm_client

    async def create_plan(self, keyword: str, intent: str, runtime: LLMRuntimeConfig) -> SearchPlan:
        system_prompt = (
            "你是一个面向数据发现任务的研究规划器。"
            "请根据用户输入输出严格 JSON，字段为："
            "search_queries(字符串数组)、extraction_focus(字符串数组)、preferred_panel_type、reasoning。"
            "preferred_panel_type 只能是 timeseries、barchart、table、mixed 之一。"
        )
        user_prompt = f"""
用户关键词: {keyword}
展示意图: {intent}

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
        return SearchPlan.model_validate(payload)

