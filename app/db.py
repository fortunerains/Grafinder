from __future__ import annotations

from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.models import Base
from app.services.auth import hash_password, parse_auth_users

settings = get_settings()
engine = create_async_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("ALTER TABLE ingestion_tasks ADD COLUMN IF NOT EXISTS owner_username VARCHAR(128)"))
        await conn.execute(text("ALTER TABLE ingestion_tasks ADD COLUMN IF NOT EXISTS source_hint TEXT"))
        await conn.execute(text("ALTER TABLE ingestion_tasks ADD COLUMN IF NOT EXISTS source_strategy VARCHAR(32) DEFAULT 'trusted_first'"))
        await conn.execute(text("ALTER TABLE documents ADD COLUMN IF NOT EXISTS crawl_engine VARCHAR(64)"))
        await conn.execute(text("ALTER TABLE source_candidates ADD COLUMN IF NOT EXISTS source_type VARCHAR(64)"))
        await conn.execute(text("ALTER TABLE source_candidates ADD COLUMN IF NOT EXISTS source_quality_score INTEGER"))
        await conn.execute(text("ALTER TABLE source_candidates ADD COLUMN IF NOT EXISTS source_quality_reason TEXT"))
        await conn.execute(text("ALTER TABLE source_candidates ADD COLUMN IF NOT EXISTS catalog_source_name VARCHAR(255)"))
        for username, password in parse_auth_users(settings.auth_users):
            await conn.execute(
                text(
                    """
                    INSERT INTO user_accounts (username, password_hash, is_active)
                    VALUES (:username, :password_hash, TRUE)
                    ON CONFLICT (username)
                    DO UPDATE SET password_hash = EXCLUDED.password_hash, is_active = TRUE, updated_at = now()
                    """
                ),
                {"username": username, "password_hash": hash_password(password)},
            )


@asynccontextmanager
async def session_scope() -> AsyncSession:
    async with SessionLocal() as session:
        yield session
