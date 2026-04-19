from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from openai import APIStatusError
from sqlalchemy.exc import SQLAlchemyError

from app.config import get_settings
from app.db import SessionLocal, init_db
from app.models import IngestionTask, TaskStatus
from app.schemas import LLMTestRequest, LLMTestResult, TaskCreate, TaskRead, TaskRefine, TaskSourceRead
from app.services.llm import LLMJsonClient
from app.services.llm_registry import ProviderRegistry
from app.services.task_runner import TaskRunner

settings = get_settings()
settings.apply_process_proxy_env()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
provider_registry = ProviderRegistry(settings)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    app.state.runner = TaskRunner(settings)
    app.state.llm_client = LLMJsonClient(settings)
    yield


app = FastAPI(title=settings.app_name, lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "index.html", {"app_name": settings.app_name})


@app.get("/api/meta")
async def meta() -> dict:
    default_provider, providers = provider_registry.list_providers()
    return {
        "default_provider": default_provider,
        "providers": [provider.model_dump() for provider in providers],
        "intent_options": ["趋势分析", "排行分析", "原始明细", "自动推荐"],
    }


@app.post("/api/tasks", response_model=TaskRead)
async def create_task(payload: TaskCreate) -> TaskRead:
    try:
        runtime = provider_registry.resolve(
            provider_name=payload.llm_provider,
            base_url_override=payload.llm_base_url,
            model_override=payload.llm_model,
            api_key_override=payload.llm_api_key,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    async with SessionLocal() as session:
        task = IngestionTask(
            keyword=payload.keyword,
            intent=payload.intent,
            source_hint=payload.source_hint,
            llm_provider=runtime.provider,
            llm_model=runtime.model,
            llm_base_url=runtime.base_url,
            status=TaskStatus.queued,
            dashboard_revision=1,
        )
        session.add(task)
        try:
            await session.commit()
            await session.refresh(task)
        except SQLAlchemyError as exc:
            await session.rollback()
            raise HTTPException(status_code=500, detail="Could not create task.") from exc

    asyncio.create_task(app.state.runner.run_task(task.id, runtime, payload.source_hint))
    task_view = await app.state.runner.get_task_view(task.id)
    return TaskRead.model_validate(task_view)


@app.post("/api/llm/test", response_model=LLMTestResult)
async def test_llm_connection(payload: LLMTestRequest) -> LLMTestResult:
    try:
        runtime = provider_registry.resolve(
            provider_name=payload.llm_provider,
            base_url_override=payload.llm_base_url,
            model_override=payload.llm_model,
            api_key_override=payload.llm_api_key,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        probe = await app.state.llm_client.test_connection(runtime)
    except APIStatusError as exc:
        detail = str(exc)
        raise HTTPException(status_code=exc.status_code if 400 <= exc.status_code < 500 else 502, detail=detail) from exc
    except RuntimeError as exc:
        message = str(exc)
        status_code = 504 if "timed out" in message.lower() else 502
        raise HTTPException(status_code=status_code, detail=message) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"LLM connection test failed: {exc}") from exc

    return LLMTestResult(
        provider=runtime.provider,
        model=runtime.model,
        base_url=runtime.base_url,
        latency_ms=int(probe.get("latency_ms", 0)),
        message=str(probe.get("message") or "Connection OK."),
    )


@app.post("/api/tasks/{task_id}/refine", response_model=TaskRead)
async def refine_task(task_id: int, payload: TaskRefine) -> TaskRead:
    try:
        task = await app.state.runner.get_task_record(task_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if task.status == TaskStatus.running:
        raise HTTPException(status_code=409, detail="This task is still running. Please wait for it to finish first.")

    try:
        runtime = provider_registry.resolve(
            provider_name=payload.llm_provider or task.llm_provider,
            base_url_override=payload.llm_base_url or task.llm_base_url,
            model_override=payload.llm_model or task.llm_model,
            api_key_override=payload.llm_api_key,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    await app.state.runner.mark_task_running_for_refinement(task_id, payload.instruction)
    asyncio.create_task(app.state.runner.refine_dashboard(task.id, payload.instruction, runtime))
    task_view = await app.state.runner.get_task_view(task.id)
    return TaskRead.model_validate(task_view)


@app.get("/api/tasks/{task_id}", response_model=TaskRead)
async def get_task(task_id: int) -> TaskRead:
    try:
        task_view = await app.state.runner.get_task_view(task_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return TaskRead.model_validate(task_view)


@app.get("/api/tasks/{task_id}/sources", response_model=list[TaskSourceRead])
async def get_task_sources(task_id: int) -> list[TaskSourceRead]:
    try:
        await app.state.runner.get_task_record(task_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return await app.state.runner.get_task_sources(task_id)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}
