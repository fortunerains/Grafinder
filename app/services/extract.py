from __future__ import annotations

from app.schemas import CrawledDocument, ExtractedItem, ExtractionEnvelope, LLMRuntimeConfig, SearchPlan
from app.services.llm import LLMJsonClient


class ExtractionService:
    def __init__(self, llm_client: LLMJsonClient):
        self.llm_client = llm_client

    async def extract(
        self,
        keyword: str,
        intent: str,
        plan: SearchPlan,
        document: CrawledDocument,
        runtime: LLMRuntimeConfig,
    ) -> ExtractionEnvelope:
        system_prompt = (
            "你是一个结构化信息抽取器。"
            "请严格输出 JSON，对网页正文提取适合进入本地数据库与 Grafana 的记录。"
            "输出字段为：document_summary、suggested_panel_type、records。"
            "records 每项包含：title、source_url、source_name、published_at、entity、metric_name、metric_value、metric_unit、summary、confidence、raw_payload。"
            "不要编造不存在的信息。"
        )
        user_prompt = f"""
关键词: {keyword}
展示意图: {intent}
抽取重点: {", ".join(plan.extraction_focus)}
来源标题: {document.title}
来源地址: {document.url}
来源站点: {document.source_name or "未知"}
来源时间: {document.published_at.isoformat() if document.published_at else "未知"}

网页正文:
{document.markdown}

要求:
- 最多抽取 6 条 records
- 如果文中出现实体、时间、数值、排行、产能、金额、装机量、市场份额等，优先抽取
- metric_value 必须是纯数字，无法确认则为 null
- suggested_panel_type 只能是 timeseries、barchart、table、mixed
- summary 要能帮助 Grafana 表格阅读
""".strip()

        payload = await self.llm_client.complete_json(runtime, system_prompt, user_prompt)
        envelope = ExtractionEnvelope.model_validate(payload)
        if envelope.records:
            return envelope

        fallback = ExtractedItem(
            title=document.title,
            source_url=document.url,
            source_name=document.source_name,
            published_at=document.published_at,
            entity=keyword,
            metric_name=None,
            metric_value=None,
            metric_unit=None,
            summary=envelope.document_summary or f"{document.title} 的抽取结果暂无明确数值字段，保留为原始明细。",
            confidence=0.3,
            raw_payload={"fallback": True},
        )
        return ExtractionEnvelope(
            document_summary=envelope.document_summary or fallback.summary,
            suggested_panel_type=envelope.suggested_panel_type or "table",
            records=[fallback],
        )

