from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from sqlalchemy import desc, func, select

from app.config import Settings
from app.db import SessionLocal
from app.models import Document, ExtractedRecord, IngestionTask, SourceCandidate, TaskStatus
from app.schemas import CrawledDocument, DashboardDesign, LLMRuntimeConfig, SearchPlan, TaskSourceRead
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

    async def run_task(self, task_id: int, runtime: LLMRuntimeConfig, source_hint: str | None = None) -> None:
        try:
            task = await self._load_task(task_id)
            await self._update_task(task_id, status=TaskStatus.running, error_message=None)
            effective_source_hint = source_hint or task.source_hint

            plan = await self.planner.create_plan(task.keyword, task.intent, runtime, effective_source_hint)
            await self._update_task(task_id, plan_payload=plan.model_dump(mode="json"))

            sources = await self.search.discover(
                plan.search_queries,
                keyword=task.keyword,
                intent=task.intent,
                source_hint=effective_source_hint,
            )
            selected_sources = self.search.select_for_crawl(
                sources,
                keyword=task.keyword,
                intent=task.intent,
                source_hint=effective_source_hint,
                max_documents=self.settings.max_documents_per_task,
            )
            await self._store_sources(task_id, sources, {source.url for source in selected_sources})

            crawled_documents = await self.crawler.crawl(selected_sources)
            stored_documents = await self._store_documents(task_id, crawled_documents)

            total_records = 0
            extraction_errors: list[str] = []
            for document in stored_documents:
                try:
                    total_records += await asyncio.wait_for(
                        self._extract_and_store(task, plan, document, runtime),
                        timeout=min(max(self.settings.network_timeout_seconds + 15, 60), 75),
                    )
                except Exception as exc:
                    logger.warning("Extraction failed for document %s in task %s", document.id, task.id, exc_info=True)
                    extraction_errors.append(f"{document.title}: {exc}")

            if total_records == 0 and extraction_errors:
                raise RuntimeError(extraction_errors[0])

            dataset_profile = await self._build_dataset_profile(task.id)
            dashboard_design, used_fallback_design = await self._design_dashboard(
                task=task,
                runtime=runtime,
                panel_hint=plan.preferred_panel_type or self._fallback_panel(task.intent),
                refinement_instruction=None,
                allow_fallback=True,
                dataset_profile=dataset_profile,
            )
            dashboard_uid, dashboard_url = await self.grafana.publish_dashboard(
                task_id=task_id,
                keyword=task.keyword,
                intent=task.intent,
                design=dashboard_design,
                time_range=self._derive_dashboard_time_range(dataset_profile),
            )

            dashboard_mode = "a fallback Grafana dashboard" if used_fallback_design else "the Grafana dashboard"
            summary = (
                f"Found {len(sources)} sources, crawled {len(stored_documents)} pages, "
                f"saved {total_records} structured records, and published {dashboard_mode}."
            )
            if extraction_errors:
                summary += f" {len(extraction_errors)} pages hit extraction issues and were skipped."
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

            dataset_profile = await self._build_dataset_profile(task.id)
            dashboard_design, _ = await self._design_dashboard(
                task=task,
                runtime=runtime,
                panel_hint=self._preferred_panel_hint(task),
                refinement_instruction=instruction,
                allow_fallback=False,
                dataset_profile=dataset_profile,
            )
            dashboard_uid, dashboard_url = await self.grafana.publish_dashboard(
                task_id=task.id,
                keyword=task.keyword,
                intent=task.intent,
                design=dashboard_design,
                time_range=self._derive_dashboard_time_range(dataset_profile),
            )
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
                "source_hint": task.source_hint,
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

    async def get_task_sources(self, task_id: int) -> list[TaskSourceRead]:
        async with SessionLocal() as session:
            rows = (
                await session.execute(
                    select(SourceCandidate, Document)
                    .outerjoin(Document, Document.source_id == SourceCandidate.id)
                    .where(SourceCandidate.task_id == task_id)
                    .order_by(SourceCandidate.rank.asc(), Document.id.asc())
                )
            ).all()

        items: list[TaskSourceRead] = []
        seen_source_ids: set[int] = set()
        for source, document in rows:
            if source.id in seen_source_ids:
                continue
            seen_source_ids.add(source.id)
            crawl_mode = "discovered_only"
            if document is not None:
                crawl_mode = "rss_summary_fallback" if "news.google.com/rss/articles/" in source.url else "page_crawled"
            items.append(
                TaskSourceRead(
                    rank=source.rank,
                    title=source.title,
                    url=source.url,
                    domain=source.domain,
                    snippet=source.snippet,
                    selected=source.selected,
                    crawl_mode=crawl_mode,
                    document_title=document.title if document else None,
                    document_source_name=document.source_name if document else None,
                    document_crawl_engine=document.crawl_engine if document else None,
                    document_published_at=document.published_at if document else None,
                    document_summary=document.summary if document else None,
                )
            )
        return items

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

    async def _store_sources(self, task_id: int, sources, selected_urls: set[str] | None = None) -> None:
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
                        selected=True if selected_urls is None else source.url in selected_urls,
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
                    crawl_engine=document.crawl_engine,
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
            crawl_engine=document.crawl_engine,
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
        dataset_profile: dict,
    ) -> tuple[DashboardDesign, bool]:
        try:
            design = await self.dashboard_designer.design_dashboard(
                keyword=task.keyword,
                intent=task.intent,
                panel_hint=panel_hint,
                dataset_profile=dataset_profile,
                runtime=runtime,
                refinement_instruction=refinement_instruction,
            )
            design = self.dashboard_designer.apply_dataset_defaults(
                task.keyword,
                task.intent,
                design,
                dataset_profile,
                force_numeric_first=refinement_instruction is None,
            )
            return design, False
        except Exception:
            if allow_fallback:
                logger.warning(
                    "Falling back to default dashboard design for task %s",
                    task.id,
                    exc_info=True,
                )
                return self.dashboard_designer.build_default_design(task.keyword, task.intent, panel_hint, dataset_profile), True
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
            numeric_rows = (
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
                    .where(
                        ExtractedRecord.task_id == task_id,
                        ExtractedRecord.metric_value.is_not(None),
                    )
                    .order_by(desc(ExtractedRecord.published_at), desc(ExtractedRecord.created_at))
                    .limit(50)
                )
            ).all()
            source_url_rows = (
                await session.execute(
                    select(SourceCandidate.url)
                    .where(SourceCandidate.task_id == task_id)
                    .order_by(SourceCandidate.rank.asc())
                )
            ).all()

        numeric_records = [
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
            for row in numeric_rows
        ]
        source_urls = [row[0] for row in source_url_rows if row[0]]

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
            "numeric_records": numeric_records,
            "preferred_numeric_series": self._pick_preferred_numeric_series(numeric_records),
            "current_numeric_comparison": self._pick_current_numeric_comparison(numeric_records),
            "current_numeric_snapshot": self._pick_current_numeric_snapshot(numeric_records),
            "data_quality_notes": self._build_data_quality_notes(numeric_records, source_urls),
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

    @staticmethod
    def _pick_preferred_numeric_series(numeric_records: list[dict]) -> dict:
        if not numeric_records:
            return {}

        series_candidates = [record for record in numeric_records if record.get("published_at")] or numeric_records
        families: dict[str, dict] = {}
        for record in series_candidates:
            family = TaskRunner._numeric_family(record)
            if family == "other_numeric":
                continue
            bucket = families.setdefault(
                family,
                {
                    "kind": family,
                    "records": [],
                    "metric_names": set(),
                    "entities": set(),
                    "units": set(),
                    "keywords": set(),
                },
            )
            bucket["records"].append(record)
            if record.get("metric_name"):
                bucket["metric_names"].add(str(record["metric_name"]))
            if record.get("entity"):
                bucket["entities"].add(str(record["entity"]))
            if record.get("metric_unit"):
                bucket["units"].add(str(record["metric_unit"]))
            bucket["keywords"].update(TaskRunner._keywords_from_record(record))

        ranked = sorted(
            families.values(),
            key=lambda item: (len(item["records"]), TaskRunner._family_priority(item["kind"])),
            reverse=True,
        )
        if not ranked:
            return {}

        selected = ranked[0]
        entity_counts: dict[str, int] = {}
        for record in selected["records"]:
            entity = str(record.get("entity") or "").strip()
            if entity:
                entity_counts[entity] = entity_counts.get(entity, 0) + 1

        top_entities = [name for name, _ in sorted(entity_counts.items(), key=lambda item: (-item[1], item[0]))[:4]]
        dominant_entity = top_entities[0] if top_entities and entity_counts[top_entities[0]] >= 3 else None
        focus_records = selected["records"]
        group_by = "none"
        focus_entities = top_entities or sorted(selected["entities"])
        if dominant_entity:
            focus_records = [record for record in selected["records"] if record.get("entity") == dominant_entity]
            focus_entities = [dominant_entity]
            focus_metric_names = {
                str(record["metric_name"])
                for record in focus_records
                if record.get("metric_name")
            }
            if len(focus_metric_names) > 1:
                group_by = "metric_name"
        elif len(top_entities) > 1:
            focus_records = [record for record in selected["records"] if record.get("entity") in top_entities]
            group_by = "entity"

        price_point_records = [record for record in focus_records if TaskRunner._is_price_point_metric(record)]
        if price_point_records:
            focus_records = price_point_records

        focus_metric_names = sorted(
            {
                str(record["metric_name"])
                for record in focus_records
                if record.get("metric_name")
            }
        )
        focus_units = sorted(
            {
                str(record["metric_unit"])
                for record in focus_records
                if record.get("metric_unit")
            }
        )
        focus_keywords = sorted(
            {
                keyword
                for record in focus_records
                for keyword in TaskRunner._keywords_from_record(record)
            }
        )
        sample_unit = next(iter(selected["units"]), None)
        if selected["kind"] == "price_level":
            panel_title = f"{dominant_entity}价格区间趋势" if dominant_entity else "价格时间趋势"
            description = "按时间查看已带发布日期的直接价格数据变化。"
            series_name = "价格"
        elif selected["kind"] == "price_change_pct":
            panel_title = "价格变化幅度趋势"
            description = "按时间查看价格涨跌幅或涨幅相关数值。"
            series_name = "涨跌幅"
        else:
            panel_title = "关键数值趋势"
            description = "按时间查看当前任务里最具代表性的数值变化。"
            series_name = "关键数值"

        return {
            "kind": selected["kind"],
            "record_count": len(focus_records),
            "metric_names": focus_metric_names,
            "entities": focus_entities,
            "units": focus_units,
            "keywords": focus_keywords,
            "panel_title": panel_title,
            "description": description,
            "series_name": f"{series_name}{f' ({sample_unit})' if sample_unit else ''}",
            "metric_operation": "avg",
            "time_grain": "day",
            "group_by": group_by,
        }

    @staticmethod
    def _pick_current_numeric_comparison(numeric_records: list[dict]) -> dict:
        if not numeric_records:
            return {}

        candidates = [record for record in numeric_records if TaskRunner._is_current_snapshot_candidate(record)]
        if not candidates:
            candidates = [record for record in numeric_records if not record.get("published_at")]
        if not candidates:
            return {}

        preferred_candidates = [record for record in candidates if TaskRunner._is_price_point_metric(record)]
        if preferred_candidates:
            candidates = preferred_candidates
        else:
            non_change_candidates = [record for record in candidates if not TaskRunner._is_change_metric(record)]
            if non_change_candidates:
                candidates = non_change_candidates

        candidates = sorted(
            candidates,
            key=lambda record: (
                TaskRunner._snapshot_metric_priority(record),
                TaskRunner._safe_datetime(record.get("published_at")) or datetime.min.replace(tzinfo=UTC),
                float(record.get("metric_value") or 0.0),
            ),
            reverse=True,
        )

        deduped: list[dict] = []
        seen_entities: set[str] = set()
        for record in candidates:
            entity = str(record.get("entity") or "").strip()
            if not entity or entity in seen_entities:
                continue
            seen_entities.add(entity)
            deduped.append(record)
            if len(deduped) >= 12:
                break

        if not deduped:
            return {}

        return {
            "panel_title": "当前重点品类价格对比",
            "description": "对比当前已抓取到的重点品类价格水平，帮助快速判断当前市场状态。",
            "metric_names": sorted({str(record["metric_name"]) for record in deduped if record.get("metric_name")}),
            "entities": [str(record["entity"]) for record in deduped if record.get("entity")],
            "units": sorted({str(record["metric_unit"]) for record in deduped if record.get("metric_unit")}),
            "keywords": sorted(
                {
                    keyword
                    for record in deduped
                    for keyword in TaskRunner._keywords_from_record(record)
                }
            ),
            "limit": len(deduped),
        }

    @staticmethod
    def _pick_current_numeric_snapshot(numeric_records: list[dict]) -> dict:
        if not numeric_records:
            return {}

        snapshot_candidates = [record for record in numeric_records if TaskRunner._is_current_snapshot_candidate(record)]
        preferred_candidates = [record for record in snapshot_candidates if TaskRunner._is_price_point_metric(record)]
        if preferred_candidates:
            snapshot_candidates = preferred_candidates
        else:
            non_change_candidates = [record for record in snapshot_candidates if not TaskRunner._is_change_metric(record)]
            if non_change_candidates:
                snapshot_candidates = non_change_candidates
        prioritized = sorted(
            snapshot_candidates or numeric_records,
            key=lambda record: (
                TaskRunner._family_priority(TaskRunner._numeric_family(record)),
                TaskRunner._snapshot_metric_priority(record),
                1 if record.get("metric_unit") else 0,
                TaskRunner._safe_datetime(record.get("published_at")) or datetime.min.replace(tzinfo=UTC),
                float(record.get("metric_value") or 0.0),
            ),
            reverse=True,
        )
        selected = prioritized[0]
        family = TaskRunner._numeric_family(selected)
        metric_label = selected.get("metric_name") or "数值"
        unit = selected.get("metric_unit")
        entity_label = selected.get("entity") or "当前重点品类"
        title = "当前相关数值"
        if family == "price_level":
            title = f"{entity_label}当前价格"
        elif family == "price_change_pct":
            title = f"{entity_label}当前涨跌幅"
        return {
            "panel_title": title,
            "description": f"展示最近一条可直接分析的数值记录：{metric_label}{f'（{unit}）' if unit else ''}。",
            "metric_names": [selected["metric_name"]] if selected.get("metric_name") else [],
            "entities": [selected["entity"]] if selected.get("entity") else [],
            "units": [unit] if unit else [],
            "keywords": TaskRunner._keywords_from_record(selected),
        }

    @staticmethod
    def _build_data_quality_notes(numeric_records: list[dict], source_urls: list[str]) -> list[str]:
        notes: list[str] = []
        if source_urls and all("news.google.com/rss/articles/" in url for url in source_urls):
            notes.append("当前来源大多是聚合摘要，直接数据页较少。")
        if not numeric_records:
            notes.append("当前抓取结果缺少可直接用于分析的数值字段。")
        elif len(numeric_records) < 3:
            notes.append("当前可用数值点较少，更适合看当前状态和明细。")
        return notes

    @staticmethod
    def _derive_dashboard_time_range(dataset_profile: dict) -> tuple[datetime | None, datetime | None]:
        time_range = dataset_profile.get("time_range") or {}
        start = TaskRunner._safe_datetime(time_range.get("min"))
        end = TaskRunner._safe_datetime(time_range.get("max"))
        if start and end:
            padding = max((end - start) / 8, timedelta(days=7))
            return start - padding, end + padding
        if end:
            return end - timedelta(days=180), end + timedelta(days=14)
        return datetime.now(UTC) - timedelta(days=365), datetime.now(UTC)

    @staticmethod
    def _numeric_family(record: dict) -> str:
        combined = " ".join(
            str(record.get(field, "") or "")
            for field in ("title", "entity", "metric_name", "metric_unit", "summary")
        ).lower()
        metric_name = str(record.get("metric_name") or "").lower()
        unit = str(record.get("metric_unit") or "").lower()
        if any(token in combined for token in ("价格", "price", "最新价", "当前价格", "现货价", "报价", "中间价", "均价", "最低价", "最高价")) or any(token in unit for token in ("元", "usd", "cny", "eur", "吨")):
            return "price_level"
        if "%" in unit or "涨幅" in metric_name or "跌幅" in metric_name or "change" in metric_name:
            return "price_change_pct"
        if any(token in combined for token in ("month", "个月", "day", "天")):
            return "duration"
        return "other_numeric"

    @staticmethod
    def _family_priority(family: str) -> int:
        priorities = {
            "price_level": 4,
            "price_change_pct": 3,
            "duration": 1,
            "other_numeric": 0,
        }
        return priorities.get(family, 0)

    @staticmethod
    def _keywords_from_record(record: dict) -> list[str]:
        text = " ".join(
            str(record.get(field, "") or "")
            for field in ("entity", "metric_name", "title")
        )
        parts = [part for part in re.split(r"[\s/,_|：:（）()【】\[\]-]+", text) if part]
        seen: set[str] = set()
        keywords: list[str] = []
        for part in parts:
            normalized = part.strip()
            if len(normalized) < 2 or normalized in seen:
                continue
            seen.add(normalized)
            keywords.append(normalized)
        return keywords[:6]

    @staticmethod
    def _is_current_snapshot_candidate(record: dict) -> bool:
        text = " ".join(
            str(record.get(field, "") or "")
            for field in ("title", "entity", "metric_name", "summary")
        ).lower()
        return any(marker in text for marker in ("当前", "最新", "现货", "latest", "spot"))

    @staticmethod
    def _is_change_metric(record: dict) -> bool:
        text = " ".join(
            str(record.get(field, "") or "")
            for field in ("metric_name", "title", "summary")
        ).lower()
        return any(
            marker in text
            for marker in ("涨跌", "涨幅", "跌幅", "change", "pct", "振幅", "amplitude", "成交量", "成交额", "量比", "内盘", "外盘", "volume", "amount")
        )

    @staticmethod
    def _is_price_point_metric(record: dict) -> bool:
        metric_name = str(record.get("metric_name") or "").lower()
        text = " ".join(
            str(record.get(field, "") or "")
            for field in ("metric_name", "title", "summary")
        ).lower()
        if TaskRunner._is_change_metric(record):
            return False
        return any(
            marker in metric_name or marker in text
            for marker in ("当前价格", "最新价", "现货价", "中间价", "均价", "最高价", "最低价", "今开", "昨收", "开盘", "收盘", "报价", "price")
        )

    @staticmethod
    def _snapshot_metric_priority(record: dict) -> int:
        metric_name = str(record.get("metric_name") or "").lower()
        if any(marker in metric_name for marker in ("当前价格", "最新价", "现货价", "中间价", "均价", "latest", "spot")):
            return 5
        if any(marker in metric_name for marker in ("收盘", "开盘", "今开", "昨收")):
            return 4
        if any(marker in metric_name for marker in ("最高价", "最低价")):
            return 3
        if TaskRunner._is_change_metric(record):
            return 1
        return 2

    @staticmethod
    def _safe_datetime(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
