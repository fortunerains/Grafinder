from __future__ import annotations

import re

from app.schemas import CrawledDocument, ExtractedItem, ExtractionEnvelope, LLMRuntimeConfig, SearchPlan
from app.services.llm import LLMJsonClient


class ExtractionService:
    _LOW_VALUE_METRIC_MARKERS = (
        "page",
        "pages",
        "word_count",
        "document_pages",
        "document_word_count",
        "页数",
        "字数",
        "阅读量",
        "浏览量",
        "点赞",
        "收藏",
        "评论",
        "转发",
        "下载量",
    )

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
        focused_markdown = self._prepare_markdown_excerpt(document.markdown, keyword, plan.extraction_focus)
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
{focused_markdown}

要求:
- 最多抽取 20 条 records
- 如果文中出现实体、时间、数值、排行、产能、金额、装机量、市场份额等，优先抽取
- 如果页面包含价格表、行情表、走势图、历史价格、最低价、最高价、中间价、均价、涨跌幅，请优先抽取这些直接数值
- 如果能从页面中识别出按日期排列的价格或指标，请把不同时间点拆成多条 records，而不是只写一条摘要
- 如果页面既有直接价格数据又有新闻解读，优先保留直接价格数据
- 不要把文档页数、字数、阅读量、点赞量、评论数、下载量这类页面元数据当作业务分析指标
- metric_value 必须是纯数字，无法确认则为 null
- suggested_panel_type 只能是 timeseries、barchart、table、mixed
- summary 要能帮助 Grafana 表格阅读
""".strip()

        payload = await self.llm_client.complete_json(runtime, system_prompt, user_prompt)
        payload = self._normalize_payload(payload)
        envelope = ExtractionEnvelope.model_validate(payload)
        envelope = ExtractionEnvelope(
            document_summary=envelope.document_summary,
            suggested_panel_type=envelope.suggested_panel_type,
            records=[record for record in envelope.records if not self._is_low_value_record(record)],
        )
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

    @staticmethod
    def _normalize_payload(payload: dict) -> dict:
        records = payload.get("records")
        if not isinstance(records, list):
            return payload

        normalized_records = []
        for record in records:
            if not isinstance(record, dict):
                continue
            raw_payload = record.get("raw_payload")
            if raw_payload is not None and not isinstance(raw_payload, dict):
                record = {**record, "raw_payload": {"value": raw_payload}}
            normalized_records.append(record)

        payload["records"] = normalized_records
        return payload

    @staticmethod
    def _prepare_markdown_excerpt(markdown: str, keyword: str, extraction_focus: list[str], max_chars: int = 5000) -> str:
        if len(markdown) <= max_chars:
            return markdown

        focus_terms = ExtractionService._focus_terms(keyword, extraction_focus)
        blocks = [block.strip() for block in markdown.split("\n\n") if block.strip()]
        scored_blocks: list[tuple[int, int, str]] = []

        for index, block in enumerate(blocks):
            score = 0
            lowered = block.lower()
            for term in focus_terms:
                if term.lower() in lowered:
                    score += 2
            if "|" in block:
                score += 3
            if any(token in block for token in ("价格", "走势", "历史价格", "最低价", "最高价", "中间价", "均价", "涨幅", "price", "trend")):
                score += 2
            if any(char.isdigit() for char in block):
                score += 1
            if score > 0:
                scored_blocks.append((score, index, block))

        selected_indices = {0}
        for _, index, _ in sorted(scored_blocks, key=lambda item: (-item[0], item[1])):
            selected_indices.add(index)
            if len("\n\n".join(blocks[i] for i in sorted(selected_indices))) >= max_chars:
                break

        excerpt = "\n\n".join(blocks[index] for index in sorted(selected_indices))
        if len(excerpt) > max_chars:
            return excerpt[:max_chars]
        return excerpt

    @staticmethod
    def _focus_terms(keyword: str, extraction_focus: list[str]) -> list[str]:
        raw = [keyword, *extraction_focus, "价格", "走势", "历史价格", "最低价", "最高价", "中间价", "均价", "涨幅", "price", "trend", "historical"]
        terms: list[str] = []
        seen: set[str] = set()
        for item in raw:
            for part in re.split(r"[\s,，;；、/:：()（）\[\]【】]+", item):
                normalized = part.strip()
                if len(normalized) < 2 or normalized in seen:
                    continue
                seen.add(normalized)
                terms.append(normalized)
        return terms[:24]

    @classmethod
    def _is_low_value_record(cls, record: ExtractedItem) -> bool:
        text = " ".join(
            str(value or "")
            for value in [record.entity, record.metric_name, record.summary, record.title]
        ).lower()
        return any(marker in text for marker in cls._LOW_VALUE_METRIC_MARKERS)
