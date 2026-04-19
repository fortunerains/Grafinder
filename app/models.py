from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Enum, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class TaskStatus(str, enum.Enum):
    queued = "queued"
    running = "running"
    completed = "completed"
    failed = "failed"


class IngestionTask(Base):
    __tablename__ = "ingestion_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    keyword: Mapped[str] = mapped_column(String(255), index=True)
    intent: Mapped[str] = mapped_column(String(120))
    source_hint: Mapped[str | None] = mapped_column(Text, nullable=True)
    llm_provider: Mapped[str] = mapped_column(String(64))
    llm_model: Mapped[str] = mapped_column(String(255))
    llm_base_url: Mapped[str] = mapped_column(Text)
    status: Mapped[TaskStatus] = mapped_column(Enum(TaskStatus, name="task_status"), default=TaskStatus.queued, index=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    dashboard_uid: Mapped[str | None] = mapped_column(String(128), nullable=True)
    dashboard_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    dashboard_revision: Mapped[int] = mapped_column(Integer, default=1)
    dashboard_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    last_refinement_instruction: Mapped[str | None] = mapped_column(Text, nullable=True)
    plan_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    sources: Mapped[list["SourceCandidate"]] = relationship(back_populates="task", cascade="all, delete-orphan")
    documents: Mapped[list["Document"]] = relationship(back_populates="task", cascade="all, delete-orphan")
    records: Mapped[list["ExtractedRecord"]] = relationship(back_populates="task", cascade="all, delete-orphan")


class SourceCandidate(Base):
    __tablename__ = "source_candidates"
    __table_args__ = (UniqueConstraint("task_id", "url", name="uq_source_task_url"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("ingestion_tasks.id", ondelete="CASCADE"), index=True)
    url: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(String(500))
    snippet: Mapped[str | None] = mapped_column(Text, nullable=True)
    domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    rank: Mapped[int] = mapped_column(Integer)
    selected: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    task: Mapped[IngestionTask] = relationship(back_populates="sources")
    documents: Mapped[list["Document"]] = relationship(back_populates="source")


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (UniqueConstraint("task_id", "content_hash", name="uq_document_task_hash"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("ingestion_tasks.id", ondelete="CASCADE"), index=True)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("source_candidates.id", ondelete="SET NULL"), nullable=True)
    url: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(String(500))
    source_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    crawl_engine: Mapped[str | None] = mapped_column(String(64), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    crawled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_markdown: Mapped[str] = mapped_column(Text)
    extraction_raw: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64))

    task: Mapped[IngestionTask] = relationship(back_populates="documents")
    source: Mapped[SourceCandidate | None] = relationship(back_populates="documents")
    records: Mapped[list["ExtractedRecord"]] = relationship(back_populates="document", cascade="all, delete-orphan")


class ExtractedRecord(Base):
    __tablename__ = "extracted_records"
    __table_args__ = (
        UniqueConstraint("task_id", "fingerprint", name="uq_record_task_fingerprint"),
        Index("ix_extracted_records_task_time", "task_id", "published_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("ingestion_tasks.id", ondelete="CASCADE"), index=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(500))
    source_url: Mapped[str] = mapped_column(Text)
    source_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    entity: Mapped[str | None] = mapped_column(String(255), nullable=True)
    metric_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    metric_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    metric_unit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    summary: Mapped[str] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(String(64))
    raw_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    task: Mapped[IngestionTask] = relationship(back_populates="records")
    document: Mapped[Document] = relationship(back_populates="records")
