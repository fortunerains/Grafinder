from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterable

from sqlalchemy import desc, func, select

from app.config import Settings
from app.db import SessionLocal
from app.models import Document, ExtractedRecord, IngestionTask, SourceCandidate, TaskStatus
from app.schemas import CrawledDocument, DashboardDesign, LLMRuntimeConfig, SearchPlan
from app.services.crawl import CrawlService
from app.services.dashboard_designer import DashboardDesignerService
from app.services.extract import ExtractionService
from app.services.grafana import GrafanaService
from app.services.llm import LLMJsonClient
from app.services.planner import PlannerService
from app.services.search import SearchService

logger = logging.getLogger(__name__)


def _fingerprint(parts: Iterable[str | None]) -> str:
    joined = "|".join(part or "" for part in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


class TaskRunner:
    def __init__(self, settings: Settings):
        llm_client = LLMJsonClient(settings)
        self.settings = settings
        self.planner = PlannerService(llm_client)
        self.search = SearchService(settings)
        self.crawler = CrawlService(settings)
        self.extractor = ExtractionService(llm_client)
        self.dashboard_designer = DashboardDesignerService(llm_client)
        self.grafana = GrafanaService(settings)

    async def run_task(self, task_id: int, runtime: LLMRuntimeConfig) -> None:
        try:
            task = await self._load_task(task_id)
            await self._update_task(task_id, status=TaskStatus.running, error_message=None)

            plan = await self.planner.create_plan(task.keyword, task.intent, runtime)
            await self._update_task(task_id, plan_payload=plan.model_dump(mode="json"))

            sources = await self.search.discover(plan.search_queries)
            await self._store_sources(task_id, sources)

            crawled_documents = await self.crawler.crawl(sources[: self.settings.max_documents_per_task])
            stored_documents = await self._store_documents(task_id, crawled_documents)

            total_records = 0
            for document in stored_documents:
                total_records += await self._extract_and_store(task, plan, document, runtime)

            dashboard_design, used_fallback_design = await self._design_dashboard(
                task=task,
                runtime=runtime,
                panel_hint=plan.preferred_panel_type or self._fallback_panel(task.intent),
                refinement_instruction=None,
                allow_fallback=True,
            )
            dashboard_uid, dashboard_url = await self.grafana.publish_dashboard(task_id=task_id, keyword=task.keyword, intent=task.intent, design=dashboard_design)

            dashboard_mode = "a fallback Grafana dashboard" if used_fallback_design else "the Grafana dashboard"
            summary = (
                f"Found {len(sources)} sources, crawled {len(stored_documents)} pages, "
                f"saved {total_records} structured records, and published {dashboard_mode}."
            )
            await self._update_task(
                task_id,
                status=TaskStatus.completed,
                dashboard_uid=dashboard_uid,
                dashboard_url=dashboard_url,
                dashboard_payload=dashboard_design.model_dump(mode="json"),
                summary=summary,
            )
        except Exception as exc:
            await self._update_task(task_id, status=TaskStatus.failed, error_message=str(exc))

    async def refine_dashboard(self, task_id: int, instruction: str, runtime: LLMRuntimeConfig) -> None:
        try:
            task = await self._load_task(task_id)
            await self._update_task(
                task_id,
                status=TaskStatus.running,
                error_message=None,
                summary="正在根据你的反馈重新生成 Grafana 面板...",
                last_refinement_instruction=instruction,
            )

            dashboard_design, _ = await self._design_dashboard(
                task=task,
                runtime=runtime,
                panel_hint=self._preferred_panel_hint(task),
                refinement_instruction=instruction,
                allow_fallback=False,
            )
            dashboard_uid, dashboard_url = await self.grafana.publish_dashboard(task_id=task.id, keyword=task.keyword, intent=task.intent, design=dashboard_design)
            next_revision = (task.dashboard_revision or 1) + 1
            summary = "已根据最新调整要求重新生成 Grafana 面板。"
            await self._update_task(
                task_id,
                status=TaskStatus.completed,
                dashboard_uid=dashboard_uid,
                dashboard_url=dashboard_url,
                dashboard_revision=next_revision,
                dashboard_payload=dashboard_design.model_dump(mode="json"),
                summary=summary,
                last_refinement_instruction=instruction,
            )
        except Exception as exc:
            await self._update_task(task_id, status=TaskStatus.failed, error_message=str(exc))

    async def get_task_view(self, task_id: int) -> dict:
        async with SessionLocal() as session:
            task = await session.get(IngestionTask, task_id)
            if task is None:
                raise ValueError(f"Task {task_id} does not exist.")

            sources_count = await session.scalar(select(func.count(SourceCandidate.id)).where(SourceCandidate.task_id == task_id))
            documents_count = await session.scalar(select(func.count(Document.id)).where(Document.task_id == task_id))
            records_count = await session.scalar(select(func.count(ExtractedRecord.id)).where(ExtractedRecord.task_id == task_id))

            return {
                "id": task.id,
                "keyword": task.keyword,
                "intent": task.intent,
                "llm_provider": task.llm_provider,
                "llm_model": task.llm_model,
                "llm_base_url": task.llm_base_url,
                "status": task.status.value,
                "summary": task.summary,
                "error_message": task.error_message,
                "dashboard_url": task.dashboard_url,
                "dashboard_revision": task.dashboard_revision,
                "dashboard_payload": task.dashboard_payload,
                "last_refinement_instruction": task.last_refinement_instruction,
                "plan_payload": task.plan_payload,
                "sources_count": int(sources_count or 0),
                "documents_count": int(documents_count or 0),
                "records_count": int(records_count or 0),
                "created_at": task.created_at,
                "updated_at": task.updated_at,
            }

    async def get_task_record(self, task_id: int) -> IngestionTask:
        return await self._load_task(task_id)

    async def mark_task_running_for_refinement(self, task_id: int, instruction: str) -> None:
        await self._update_task(
            task_id,
            status=TaskStatus.running,
            error_message=None,
            summary="正在根据你的反馈重新生成 Grafana 面板...",
            last_refinement_instruction=instruction,
        )

    async def _load_task(self, task_id: int) -> IngestionTask:
        async with SessionLocal() as session:
            task = await session.get(IngestionTask, task_id)
            if task is None:
                raise ValueError(f"Task {task_id} does not exist.")
            return task

    async def _update_task(self, task_id: int, **fields) -> None:
        async with SessionLocal() as session:
            task = await session.get(IngestionTask, task_id)
            if task is None:
                raise ValueError(f"Task {task_id} does not exist.")
            for key, value in fields.items():
                setattr(task, key, value)
            await session.commit()

    async def _store_sources(self, task_id: int, sources) -> None:
        async with SessionLocal() as session:
            for source in sources:
                session.add(
                    SourceCandidate(
                        task_id=task_id,
                        url=source.url,
                        title=source.title,
                        snippet=source.snippet,
                        domain=source.domain,
                        rank=source.rank,
                        selected=True,
                    )
                )
            await session.commit()

    async def _store_documents(
        self,
        task_id: int,
        crawled_documents: list[CrawledDocument],
    ) -> list[Document]:
        source_id_by_url = {source.url: source.id for source in await self._load_source_rows(task_id)}
        stored: list[Document] = []
        async with SessionLocal() as session:
            for document in crawled_documents:
                document_row = Document(
                    task_id=task_id,
                    source_id=source_id_by_url.get(document.url),
                    url=document.url,
                    title=document.title,
                    source_name=document.source_name,
                    published_at=document.published_at,
                    content_markdown=document.markdown,
                    content_hash=_fingerprint([document.url, document.title, document.markdown[:1000]]),
                )
                session.add(document_row)
                stored.append(document_row)
            await session.commit()
            for row in stored:
                await session.refresh(row)
        return stored

    async def _load_source_rows(self, task_id: int) -> list[SourceCandidate]:
        async with SessionLocal() as session:
            result = await session.scalars(
                select(SourceCandidate).where(SourceCandidate.task_id == task_id).order_by(SourceCandidate.rank.asc())
            )
            return list(result)

    async def _extract_and_store(
        self,
        task: IngestionTask,
        plan: SearchPlan,
        document: Document,
        runtime: LLMRuntimeConfig,
    ) -> int:
        crawled = CrawledDocument(
            url=document.url,
            title=document.title,
            source_name=document.source_name,
            published_at=document.published_at,
            markdown=document.content_markdown,
            raw_metadata=document.extraction_raw or {},
        )
        extraction = await self.extractor.extract(task.keyword, task.intent, plan, crawled, runtime)

        count = 0
        async with SessionLocal() as session:
            document_row = await session.get(Document, document.id)
            if document_row is None:
                raise ValueError(f"Document {document.id} does not exist.")

            document_row.summary = extraction.document_summary
            document_row.extraction_raw = extraction.model_dump(mode="json")

            seen: set[str] = set()
            for record in extraction.records:
                fingerprint = _fingerprint(
                    [
                        str(task.id),
                        document.url,
                        record.title,
                        record.entity,
                        record.metric_name,
                        str(record.metric_value) if record.metric_value is not None else None,
                        record.summary,
                    ]
                )
                if fingerprint in seen:
                    continue
                seen.add(fingerprint)
                session.add(
                    ExtractedRecord(
                        task_id=task.id,
                        document_id=document.id,
                        title=record.title,
                        source_url=record.source_url,
                        source_name=record.source_name,
                        published_at=record.published_at,
                        entity=record.entity,
                        metric_name=record.metric_name,
                        metric_value=record.metric_value,
                        metric_unit=record.metric_unit,
                        summary=record.summary,
                        fingerprint=fingerprint,
                        raw_payload=record.raw_payload,
                    )
                )
                count += 1

            await session.commit()

        return count

    async def _design_dashboard(
        self,
        task: IngestionTask,
        runtime: LLMRuntimeConfig,
        panel_hint: str,
        refinement_instruction: str | None,
        allow_fallback: bool,
    ) -> tuple[DashboardDesign, bool]:
        dataset_profile = await self._build_dataset_profile(task.id)
        try:
            design = await self.dashboard_designer.design_dashboard(
                keyword=task.keyword,
                intent=task.intent,
                panel_hint=panel_hint,
                dataset_profile=dataset_profile,
                runtime=runtime,
                refinement_instruction=refinement_instruction,
            )
            return design, False
        except Exception:
            if allow_fallback:
                logger.warning(
                    "Falling back to default dashboard design for task %s",
                    task.id,
                    exc_info=True,
                )
                return self.dashboard_designer.build_default_design(task.keyword, task.intent, panel_hint), True
            raise

    async def _build_dataset_profile(self, task_id: int) -> dict:
        async with SessionLocal() as session:
            records_count = await session.scalar(select(func.count(ExtractedRecord.id)).where(ExtractedRecord.task_id == task_id))
            numeric_count = await session.scalar(
                select(func.count(ExtractedRecord.id)).where(
                    ExtractedRecord.task_id == task_id,
                    ExtractedRecord.metric_value.is_not(None),
                )
            )
            dated_count = await session.scalar(
                select(func.count(ExtractedRecord.id)).where(
                    ExtractedRecord.task_id == task_id,
                    ExtractedRecord.published_at.is_not(None),
                )
            )
            time_bounds = (
                await session.execute(
                    select(
                        func.min(ExtractedRecord.published_at),
                        func.max(ExtractedRecord.published_at),
                    ).where(ExtractedRecord.task_id == task_id)
                )
            ).one()
            entity_rows = (
                await session.execute(
                    select(ExtractedRecord.entity, func.count(ExtractedRecord.id).label("count"))
                    .where(ExtractedRecord.task_id == task_id, ExtractedRecord.entity.is_not(None))
                    .group_by(ExtractedRecord.entity)
                    .order_by(desc("count"))
                    .limit(8)
                )
            ).all()
            metric_rows = (
                await session.execute(
                    select(ExtractedRecord.metric_name, func.count(ExtractedRecord.id).label("count"))
                    .where(ExtractedRecord.task_id == task_id, ExtractedRecord.metric_name.is_not(None))
                    .group_by(ExtractedRecord.metric_name)
                    .order_by(desc("count"))
                    .limit(8)
                )
            ).all()
            source_rows = (
                await session.execute(
                    select(ExtractedRecord.source_name, func.count(ExtractedRecord.id).label("count"))
                    .where(ExtractedRecord.task_id == task_id, ExtractedRecord.source_name.is_not(None))
                    .group_by(ExtractedRecord.source_name)
                    .order_by(desc("count"))
                    .limit(8)
                )
            ).all()
            sample_rows = (
                await session.execute(
                    select(
                        ExtractedRecord.published_at,
                        ExtractedRecord.title,
                        ExtractedRecord.entity,
                        ExtractedRecord.metric_name,
                        ExtractedRecord.metric_value,
                        ExtractedRecord.metric_unit,
                        ExtractedRecord.source_name,
                        ExtractedRecord.summary,
                    )
                    .where(ExtractedRecord.task_id == task_id)
                    .order_by(desc(ExtractedRecord.published_at), desc(ExtractedRecord.created_at))
                    .limit(12)
                )
            ).all()

        return {
            "record_count": int(records_count or 0),
            "metric_value_count": int(numeric_count or 0),
            "published_time_count": int(dated_count or 0),
            "time_range": {
                "min": time_bounds[0].isoformat() if time_bounds[0] else None,
                "max": time_bounds[1].isoformat() if time_bounds[1] else None,
            },
            "top_entities": [{"name": row[0], "count": row[1]} for row in entity_rows if row[0]],
            "top_metrics": [{"name": row[0], "count": row[1]} for row in metric_rows if row[0]],
            "top_sources": [{"name": row[0], "count": row[1]} for row in source_rows if row[0]],
            "sample_rows": [
                {
                    "published_at": row[0].isoformat() if row[0] else None,
                    "title": row[1],
                    "entity": row[2],
                    "metric_name": row[3],
                    "metric_value": row[4],
                    "metric_unit": row[5],
                    "source_name": row[6],
                    "summary": row[7],
                }
                for row in sample_rows
            ],
        }

    @staticmethod
    def _fallback_panel(intent: str) -> str:
        if "趋势" in intent:
            return "timeseries"
        if "排行" in intent:
            return "barchart"
        if "原始" in intent:
            return "table"
        return "mixed"

    @staticmethod
    def _preferred_panel_hint(task: IngestionTask) -> str:
        if task.plan_payload and isinstance(task.plan_payload, dict):
            preferred = task.plan_payload.get("preferred_panel_type")
            if preferred:
                return str(preferred)
        return TaskRunner._fallback_panel(task.intent)
