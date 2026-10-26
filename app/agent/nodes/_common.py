"""Shared helpers for LangGraph node logging and persistence."""

from __future__ import annotations

import difflib
import json
import time
from typing import Any, Dict, List, Optional

from app.core.logging import get_logger
from app.db.session import append_iteration_record, update_run_stage

logger = get_logger(__name__)

STAGE_PROGRESS = {
    "parsing": 10,
    "generating": 30,
    "validating": 50,
    "fixing": 60,
    "saving": 75,
    "ci_running": 88,
    "collecting": 95,
    "completed": 100,
    "failed": 100,
}


def json_safe(value: Any) -> str:
    """Serialize values to compact JSON without failing the workflow."""
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        return str(value)


def compute_unified_diff(
    old: str, new: str, fromfile: str = "before", tofile: str = "after"
) -> str:
    """Return a unified diff for two code strings."""
    if old == new:
        return ""
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=fromfile,
            tofile=tofile,
        )
    )


def record_stage(
    state: Dict[str, Any],
    stage: str,
    started_at: float,
    input_summary: str,
    output_summary: str,
    *,
    status: str = "ok",
    errors: Optional[List[str]] = None,
    code_snapshot: Optional[str] = None,
    diff: Optional[str] = None,
    current_stage: Optional[str] = None,
    progress: Optional[int] = None,
    run_status: Optional[str] = None,
    updates: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Append a history event and persist it to the DB; return the event."""
    elapsed_ms = round((time.perf_counter() - started_at) * 1000.0, 2)
    run_id = str(state.get("run_id", ""))
    iteration = int(state.get("iteration", 0))
    event: Dict[str, Any] = {
        "iteration": iteration,
        "stage": stage,
        "status": status,
        "input_summary": input_summary[:1500],
        "output_summary": output_summary[:2500],
        "duration_ms": elapsed_ms,
        "timestamp": None,
    }
    if errors:
        event["errors"] = errors[:50]
    if code_snapshot is not None:
        event["test_code_snapshot"] = code_snapshot
    if diff:
        event["diff"] = diff

    history = list(state.get("history") or [])
    history.append(event)
    state["history"] = history
    state["current_stage"] = current_stage or stage
    state["progress"] = progress if progress is not None else STAGE_PROGRESS.get(stage, 50)
    if run_status:
        state["status"] = run_status

    try:
        append_iteration_record(
            run_id=run_id,
            iteration_number=iteration,
            stage=stage,
            status=status,
            input_summary=event["input_summary"],
            output_summary=event["output_summary"],
            duration_ms=elapsed_ms,
            test_code_snapshot=code_snapshot,
            diff=diff,
            errors=errors,
        )
        update_values = updates or {}
        update_run_stage(
            run_id=run_id,
            stage=state["current_stage"],
            progress=state["progress"],
            status=state.get("status"),
            iteration=iteration,
            **update_values,
        )
    except Exception:
        logger.exception("Failed to persist agent event", extra={"run_id": run_id, "stage": stage})
    return event
