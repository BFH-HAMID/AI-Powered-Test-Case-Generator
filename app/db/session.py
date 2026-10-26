"""Database engine, session factory, and CRUD helper functions."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Generator, List, Optional

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.db.models import Base, RunFeedback, RunIteration, TestRun

_ENGINE = None
_SESSION_FACTORY = None


def get_engine(database_url: Optional[str] = None):
    """Create or return singleton SQLAlchemy engine."""
    global _ENGINE
    settings = get_settings()
    url = database_url or settings.database_url
    if _ENGINE is None or str(_ENGINE.url) != url:
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        _ENGINE = create_engine(url, connect_args=connect_args, future=True)
    return _ENGINE


def get_session_factory(database_url: Optional[str] = None) -> sessionmaker:
    """Return configured sessionmaker."""
    global _SESSION_FACTORY
    engine = get_engine(database_url)
    if _SESSION_FACTORY is None:
        _SESSION_FACTORY = sessionmaker(
            bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
        )
    return _SESSION_FACTORY


def reset_db_engine(database_url: Optional[str] = None) -> None:
    """Reset cached engine and session factory (used in tests)."""
    global _ENGINE, _SESSION_FACTORY
    if _ENGINE is not None:
        _ENGINE.dispose()
    _ENGINE = None
    _SESSION_FACTORY = None
    if database_url:
        init_db(database_url)


def init_db(database_url: Optional[str] = None) -> None:
    """Create all tables if they do not exist."""
    engine = get_engine(database_url)
    Base.metadata.create_all(bind=engine)


@contextmanager
def db_session_scope() -> Generator[Session, None, None]:
    """Provide a transactional scope around a series of operations."""
    factory = get_session_factory()
    session: Session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency for database sessions."""
    factory = get_session_factory()
    session: Session = factory()
    try:
        yield session
    finally:
        session.close()


def create_run_record(
    run_id: str,
    requirement: str,
    source_code: Optional[str],
    module_name: str,
    language: str,
    framework: str,
    repo_url: Optional[str],
    branch: Optional[str],
    options: Dict[str, Any],
    max_iterations: int = 3,
) -> TestRun:
    """Persist a new TestRun record."""
    with db_session_scope() as session:
        run = TestRun(
            id=run_id,
            requirement=requirement,
            source_code=source_code,
            module_name=module_name,
            language=language.lower(),
            framework=framework.lower(),
            repo_url=repo_url,
            branch=branch,
            options_json=options,
            status="pending",
            current_stage="queued",
            progress=0,
            iteration=0,
            max_iterations=max_iterations,
        )
        session.add(run)
        session.flush()
        return run


def update_run_stage(
    run_id: str,
    stage: str,
    progress: int,
    status: Optional[str] = None,
    iteration: Optional[int] = None,
    parsed_spec: Optional[Dict[str, Any]] = None,
    test_code: Optional[str] = None,
    validation_report: Optional[Dict[str, Any]] = None,
    ci_result: Optional[Dict[str, Any]] = None,
    final_result: Optional[Dict[str, Any]] = None,
    file_path: Optional[str] = None,
    commit_sha: Optional[str] = None,
    pr_url: Optional[str] = None,
    error_message: Optional[str] = None,
) -> None:
    """Update stage, progress, and partial or final outputs on a TestRun."""
    with db_session_scope() as session:
        run = session.get(TestRun, run_id)
        if not run:
            return
        run.current_stage = stage
        run.progress = max(0, min(100, progress))
        if status is not None:
            run.status = status
        if iteration is not None:
            run.iteration = iteration
        if parsed_spec is not None:
            run.parsed_spec_json = parsed_spec
        if test_code is not None:
            run.test_code = test_code
        if validation_report is not None:
            run.validation_report_json = validation_report
        if ci_result is not None:
            run.ci_result_json = ci_result
        if final_result is not None:
            run.final_result_json = final_result
        if file_path is not None:
            run.file_path = file_path
        if commit_sha is not None:
            run.commit_sha = commit_sha
        if pr_url is not None:
            run.pr_url = pr_url
        if error_message is not None:
            run.error_message = error_message
        run.updated_at = datetime.now(timezone.utc)


def append_iteration_record(
    run_id: str,
    iteration_number: int,
    stage: str,
    status: str,
    input_summary: str,
    output_summary: str,
    duration_ms: float,
    test_code_snapshot: Optional[str] = None,
    diff: Optional[str] = None,
    errors: Optional[List[str]] = None,
) -> RunIteration:
    """Append an iteration/stage execution entry to the DB."""
    with db_session_scope() as session:
        entry = RunIteration(
            run_id=run_id,
            iteration_number=iteration_number,
            stage=stage,
            status=status,
            input_summary=input_summary,
            output_summary=output_summary,
            test_code_snapshot=test_code_snapshot,
            diff=diff,
            errors_json=errors or [],
            duration_ms=round(duration_ms, 2),
        )
        session.add(entry)
        session.flush()
        return entry


def get_run_with_details(run_id: str) -> Optional[Dict[str, Any]]:
    """Fetch a run along with its ordered iterations and feedbacks as a dictionary."""
    with db_session_scope() as session:
        run = session.get(TestRun, run_id)
        if not run:
            return None
        iterations = [
            {
                "id": it.id,
                "iteration": it.iteration_number,
                "stage": it.stage,
                "status": it.status,
                "input_summary": it.input_summary,
                "output_summary": it.output_summary,
                "test_code_snapshot": it.test_code_snapshot,
                "diff": it.diff,
                "errors": it.errors_json or [],
                "duration_ms": it.duration_ms,
                "timestamp": it.created_at.isoformat() if it.created_at else None,
            }
            for it in run.iterations
        ]
        feedbacks = [
            {
                "id": fb.id,
                "rating": fb.rating,
                "comment": fb.comment,
                "helpful_tags": fb.helpful_tags_json or [],
                "created_at": fb.created_at.isoformat() if fb.created_at else None,
            }
            for fb in run.feedbacks
        ]
        return {
            "run_id": run.id,
            "requirement": run.requirement,
            "source_code": run.source_code,
            "module_name": run.module_name,
            "language": run.language,
            "framework": run.framework,
            "repo_url": run.repo_url,
            "branch": run.branch,
            "options": run.options_json or {},
            "status": run.status,
            "current_stage": run.current_stage,
            "progress": run.progress,
            "iteration": run.iteration,
            "max_iterations": run.max_iterations,
            "parsed_spec": run.parsed_spec_json,
            "test_code": run.test_code,
            "validation_report": run.validation_report_json,
            "ci_result": run.ci_result_json,
            "final_result": run.final_result_json,
            "file_path": run.file_path,
            "commit_sha": run.commit_sha,
            "pr_url": run.pr_url,
            "error_message": run.error_message,
            "created_at": run.created_at.isoformat() if run.created_at else None,
            "updated_at": run.updated_at.isoformat() if run.updated_at else None,
            "history": iterations,
            "feedbacks": feedbacks,
        }


def list_runs(limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
    """Return recent test generation runs ordered by creation time descending."""
    with db_session_scope() as session:
        stmt = select(TestRun).order_by(TestRun.created_at.desc()).offset(offset).limit(limit)
        rows = session.scalars(stmt).all()
        items = []
        for r in rows:
            summary = (r.final_result_json or {}).get("summary", {})
            items.append(
                {
                    "run_id": r.id,
                    "requirement_preview": (
                        r.requirement[:120] + "..." if len(r.requirement) > 120 else r.requirement
                    ),
                    "module_name": r.module_name,
                    "language": r.language,
                    "framework": r.framework,
                    "status": r.status,
                    "current_stage": r.current_stage,
                    "progress": r.progress,
                    "iteration": r.iteration,
                    "max_iterations": r.max_iterations,
                    "summary": summary,
                    "file_path": r.file_path,
                    "commit_sha": r.commit_sha,
                    "pr_url": r.pr_url,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                    "updated_at": r.updated_at.isoformat() if r.updated_at else None,
                }
            )
        return items


def save_feedback_record(
    run_id: str,
    rating: int,
    comment: Optional[str] = None,
    helpful_tags: Optional[List[str]] = None,
) -> Optional[Dict[str, Any]]:
    """Store user rating and feedback for a completed run."""
    with db_session_scope() as session:
        run = session.get(TestRun, run_id)
        if not run:
            return None
        fb = RunFeedback(
            run_id=run_id,
            rating=rating,
            comment=comment,
            helpful_tags_json=helpful_tags or [],
        )
        session.add(fb)
        session.flush()
        return {
            "id": fb.id,
            "run_id": fb.run_id,
            "rating": fb.rating,
            "comment": fb.comment,
            "helpful_tags": fb.helpful_tags_json or [],
            "created_at": fb.created_at.isoformat() if fb.created_at else None,
        }
