from __future__ import annotations

import asyncio
import csv
import io
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from openai import APIStatusError
from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.config import get_settings
from app.db import SessionLocal, init_db
from app.models import ExtractedRecord, IngestionTask, TaskStatus, UserAccount, UserLLMSettings
from app.schemas import (
    LLMTestRequest,
    LLMTestResult,
    LLMRuntimeConfig,
    LoginRequest,
    RegisterRequest,
    SessionRead,
    TaskCreate,
    TaskRead,
    TaskRefine,
    TaskSourceRead,
    UserLLMSettingsRead,
    UserLLMSettingsUpdate,
)
from app.services.auth import SESSION_COOKIE_NAME, create_session_token, hash_password, read_session_username, verify_password
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


async def _get_active_username(request: Request) -> str | None:
    if not settings.auth_enabled:
        return "local"

    username = read_session_username(request.cookies.get(SESSION_COOKIE_NAME), settings)
    if not username:
        return None

    async with SessionLocal() as session:
        user = await session.scalar(
            select(UserAccount).where(UserAccount.username == username, UserAccount.is_active.is_(True))
        )
    return user.username if user else None


async def require_current_username(request: Request) -> str:
    username = await _get_active_username(request)
    if not username:
        raise HTTPException(status_code=401, detail="请先登录。")
    return username


async def _get_user_account(username: str) -> UserAccount:
    async with SessionLocal() as session:
        user = await session.scalar(select(UserAccount).where(UserAccount.username == username))
        if user is None and not settings.auth_enabled:
            user = UserAccount(username=username, password_hash="", is_active=True)
            session.add(user)
            await session.commit()
            await session.refresh(user)
        if user is None:
            raise HTTPException(status_code=401, detail="当前用户不存在，请重新登录。")
        return user


async def _get_user_llm_settings(username: str) -> UserLLMSettings | None:
    user = await _get_user_account(username)
    async with SessionLocal() as session:
        return await session.scalar(select(UserLLMSettings).where(UserLLMSettings.user_id == user.id))


def _settings_response(record: UserLLMSettings | None) -> UserLLMSettingsRead:
    if record is None:
        return UserLLMSettingsRead()
    return UserLLMSettingsRead(
        llm_provider=record.llm_provider,
        llm_model=record.llm_model,
        llm_model_preset=record.llm_model_preset,
        llm_base_url=record.llm_base_url,
        has_api_key=bool(record.llm_api_key),
    )


async def _resolve_runtime_for_user(
    username: str,
    payload: TaskCreate | TaskRefine | LLMTestRequest,
) -> LLMRuntimeConfig:
    saved = await _get_user_llm_settings(username)
    try:
        return provider_registry.resolve(
            provider_name=payload.llm_provider or (saved.llm_provider if saved else None),
            base_url_override=payload.llm_base_url or (saved.llm_base_url if saved else None),
            model_override=payload.llm_model or (saved.llm_model if saved else None),
            api_key_override=payload.llm_api_key or (saved.llm_api_key if saved else None),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _ensure_task_owner(task: IngestionTask, username: str) -> None:
    owner_username = getattr(task, "owner_username", None)
    if owner_username and owner_username != username:
        raise HTTPException(status_code=404, detail=f"Task {task.id} does not exist.")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "index.html", {"app_name": settings.app_name})


@app.get("/api/session", response_model=SessionRead)
async def get_session(request: Request) -> SessionRead:
    username = await _get_active_username(request)
    return SessionRead(authenticated=bool(username), username=username)


@app.post("/api/login", response_model=SessionRead)
async def login(payload: LoginRequest, response: Response) -> SessionRead:
    async with SessionLocal() as session:
        user = await session.scalar(select(UserAccount).where(UserAccount.username == payload.username))

    if user is None or not user.is_active or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="用户名或密码不正确。")

    response.set_cookie(
        SESSION_COOKIE_NAME,
        create_session_token(user.username, settings),
        max_age=settings.auth_session_max_age_seconds,
        httponly=True,
        samesite="lax",
        secure=settings.auth_cookie_secure,
    )
    return SessionRead(authenticated=True, username=user.username)


@app.post("/api/register", response_model=SessionRead)
async def register(payload: RegisterRequest, response: Response) -> SessionRead:
    if not settings.auth_allow_registration:
        raise HTTPException(status_code=403, detail="当前服务未开放注册。")

    async with SessionLocal() as session:
        existing = await session.scalar(select(UserAccount.id).where(UserAccount.username == payload.username))
        if existing is not None:
            raise HTTPException(status_code=409, detail="用户名已存在，请换一个。")

        user = UserAccount(
            username=payload.username,
            password_hash=hash_password(payload.password),
            is_active=True,
        )
        session.add(user)
        try:
            await session.commit()
            await session.refresh(user)
        except IntegrityError as exc:
            await session.rollback()
            raise HTTPException(status_code=409, detail="用户名已存在，请换一个。") from exc

    response.set_cookie(
        SESSION_COOKIE_NAME,
        create_session_token(user.username, settings),
        max_age=settings.auth_session_max_age_seconds,
        httponly=True,
        samesite="lax",
        secure=settings.auth_cookie_secure,
    )
    return SessionRead(authenticated=True, username=user.username)


@app.post("/api/logout", response_model=SessionRead)
async def logout(response: Response) -> SessionRead:
    response.delete_cookie(SESSION_COOKIE_NAME, httponly=True, samesite="lax", secure=settings.auth_cookie_secure)
    return SessionRead(authenticated=False, username=None)


@app.get("/api/meta")
async def meta(username: str = Depends(require_current_username)) -> dict:
    default_provider, providers = provider_registry.list_providers()
    return {
        "default_provider": default_provider,
        "providers": [provider.model_dump() for provider in providers],
        "intent_options": ["趋势分析", "排行分析", "原始明细", "自动推荐"],
    }


@app.get("/api/me/llm-settings", response_model=UserLLMSettingsRead)
async def get_llm_settings(username: str = Depends(require_current_username)) -> UserLLMSettingsRead:
    return _settings_response(await _get_user_llm_settings(username))


@app.put("/api/me/llm-settings", response_model=UserLLMSettingsRead)
async def update_llm_settings(
    payload: UserLLMSettingsUpdate,
    username: str = Depends(require_current_username),
) -> UserLLMSettingsRead:
    user = await _get_user_account(username)
    async with SessionLocal() as session:
        record = await session.scalar(select(UserLLMSettings).where(UserLLMSettings.user_id == user.id))
        if record is None:
            record = UserLLMSettings(user_id=user.id)
            session.add(record)

        record.llm_provider = payload.llm_provider
        record.llm_model = payload.llm_model
        record.llm_model_preset = payload.llm_model_preset
        record.llm_base_url = payload.llm_base_url
        if payload.clear_api_key:
            record.llm_api_key = None
        elif payload.llm_api_key:
            record.llm_api_key = payload.llm_api_key

        await session.commit()
        await session.refresh(record)
        return _settings_response(record)


@app.delete("/api/me/llm-settings", response_model=UserLLMSettingsRead)
async def delete_llm_settings(username: str = Depends(require_current_username)) -> UserLLMSettingsRead:
    user = await _get_user_account(username)
    async with SessionLocal() as session:
        record = await session.scalar(select(UserLLMSettings).where(UserLLMSettings.user_id == user.id))
        if record is not None:
            await session.delete(record)
            await session.commit()
    return UserLLMSettingsRead()


@app.post("/api/tasks", response_model=TaskRead)
async def create_task(payload: TaskCreate, username: str = Depends(require_current_username)) -> TaskRead:
    runtime = await _resolve_runtime_for_user(username, payload)

    async with SessionLocal() as session:
        task = IngestionTask(
            owner_username=username,
            keyword=payload.keyword,
            intent=payload.intent,
            source_hint=payload.source_hint,
            source_strategy=payload.source_strategy,
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
async def test_llm_connection(
    payload: LLMTestRequest,
    username: str = Depends(require_current_username),
) -> LLMTestResult:
    runtime = await _resolve_runtime_for_user(username, payload)

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
async def refine_task(
    task_id: int,
    payload: TaskRefine,
    username: str = Depends(require_current_username),
) -> TaskRead:
    try:
        task = await app.state.runner.get_task_record(task_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    _ensure_task_owner(task, username)
    if task.status == TaskStatus.running:
        raise HTTPException(status_code=409, detail="This task is still running. Please wait for it to finish first.")

    saved = await _get_user_llm_settings(username)
    try:
        runtime = provider_registry.resolve(
            provider_name=payload.llm_provider or task.llm_provider,
            base_url_override=payload.llm_base_url or task.llm_base_url,
            model_override=payload.llm_model or task.llm_model,
            api_key_override=payload.llm_api_key or (saved.llm_api_key if saved else None),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    await app.state.runner.mark_task_running_for_refinement(task_id, payload.instruction)
    asyncio.create_task(app.state.runner.refine_dashboard(task.id, payload.instruction, runtime))
    task_view = await app.state.runner.get_task_view(task.id)
    return TaskRead.model_validate(task_view)


@app.get("/api/tasks/{task_id}", response_model=TaskRead)
async def get_task(task_id: int, username: str = Depends(require_current_username)) -> TaskRead:
    try:
        task = await app.state.runner.get_task_record(task_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    _ensure_task_owner(task, username)
    try:
        task_view = await app.state.runner.get_task_view(task_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return TaskRead.model_validate(task_view)


@app.get("/api/tasks/{task_id}/sources", response_model=list[TaskSourceRead])
async def get_task_sources(task_id: int, username: str = Depends(require_current_username)) -> list[TaskSourceRead]:
    try:
        task = await app.state.runner.get_task_record(task_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    _ensure_task_owner(task, username)
    return await app.state.runner.get_task_sources(task_id)


@app.get("/api/tasks/{task_id}/records.csv")
async def export_task_records_csv(task_id: int, username: str = Depends(require_current_username)) -> Response:
    try:
        task = await app.state.runner.get_task_record(task_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    _ensure_task_owner(task, username)

    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(ExtractedRecord)
                .where(ExtractedRecord.task_id == task_id)
                .order_by(desc(ExtractedRecord.published_at), desc(ExtractedRecord.created_at))
            )
        ).scalars().all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "id",
            "published_at",
            "created_at",
            "title",
            "entity",
            "metric_name",
            "metric_value",
            "metric_unit",
            "source_name",
            "source_url",
            "summary",
        ]
    )
    for record in rows:
        writer.writerow(
            [
                record.id,
                record.published_at.isoformat() if record.published_at else "",
                record.created_at.isoformat() if record.created_at else "",
                record.title,
                record.entity or "",
                record.metric_name or "",
                record.metric_value if record.metric_value is not None else "",
                record.metric_unit or "",
                record.source_name or "",
                record.source_url,
                record.summary,
            ]
        )

    filename = f"grafinder-task-{task.id}.csv"
    return Response(
        content=output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}
