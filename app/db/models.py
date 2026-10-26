"""SQLAlchemy ORM models for test generation runs, iterations, and feedback."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utc_now() -> datetime:
    """Return current UTC datetime."""
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    """Base declarative class for SQLAlchemy models."""


class TestRun(Base):
    """Represents a single test generation workflow run."""

    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, index=True)
    requirement: Mapped[str] = mapped_column(Text, nullable=False)
    source_code: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    module_name: Mapped[str] = mapped_column(String(128), default="target_module")
    language: Mapped[str] = mapped_column(String(32), default="python")
    framework: Mapped[str] = mapped_column(String(32), default="pytest")
    repo_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    branch: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    options_json: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)

    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    current_stage: Mapped[str] = mapped_column(String(64), default="queued")
    progress: Mapped[int] = mapped_column(Integer, default=0)
    iteration: Mapped[int] = mapped_column(Integer, default=0)
    max_iterations: Mapped[int] = mapped_column(Integer, default=3)

    parsed_spec_json: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    test_code: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    validation_report_json: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    ci_result_json: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    final_result_json: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)

    file_path: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    commit_sha: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    pr_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    iterations: Mapped[List["RunIteration"]] = relationship(
        "RunIteration",
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="RunIteration.id",
    )
    feedbacks: Mapped[List["RunFeedback"]] = relationship(
        "RunFeedback",
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="RunFeedback.id",
    )


class RunIteration(Base):
    """Represents a node transition or fix attempt in a test generation run."""

    __tablename__ = "iterations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("runs.id", ondelete="CASCADE"), index=True
    )
    iteration_number: Mapped[int] = mapped_column(Integer, default=0)
    stage: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="ok")
    input_summary: Mapped[str] = mapped_column(Text, default="")
    output_summary: Mapped[str] = mapped_column(Text, default="")
    test_code_snapshot: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    diff: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    errors_json: Mapped[List[str]] = mapped_column(JSON, default=list)
    duration_ms: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    run: Mapped["TestRun"] = relationship("TestRun", back_populates="iterations")


class RunFeedback(Base):
    """Stores user ratings and feedback on generated test cases."""

    __tablename__ = "feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("runs.id", ondelete="CASCADE"), index=True
    )
    rating: Mapped[int] = mapped_column(Integer, nullable=False)
    comment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    helpful_tags_json: Mapped[List[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    run: Mapped["TestRun"] = relationship("TestRun", back_populates="feedbacks")
