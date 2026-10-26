"""Results aggregation node: summaries, per-test markers, logs, auto-fixes, and final status."""

from __future__ import annotations

import re
import time
from typing import Any, Dict, List

from app.agent.nodes._common import record_stage
from app.api.schemas import ResultSummarySchema
from app.core.logging import get_logger
from app.db.session import update_run_stage
from app.tools.github_tool import post_pr_summary_comment

logger = get_logger(__name__)


def collect_results_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """LangGraph node: aggregate validation, dry-run, CI, and correction history for UI/export."""
    started = time.perf_counter()
    history = list(state.get("history") or [])
    validation = state.get("validation_report") or {}
    dry = validation.get("dry_run_results") or {}
    ci = state.get("ci_result") or {}
    ci_cases = ci.get("test_cases") or []
    dry_cases = dry.get("test_cases") or []
    test_cases = ci_cases if ci_cases else dry_cases

    total = int(ci.get("total", 0) or dry.get("total", 0) or len(test_cases))
    passed = int(ci.get("passed", 0) or dry.get("passed", 0))
    failed = int(ci.get("failed", 0) or dry.get("failed", 0))
    skipped = int(ci.get("skipped", 0) or dry.get("skipped", 0))
    coverage = float(ci.get("coverage_pct", 0.0) or dry.get("coverage_pct", 0.0))
    duration = float(ci.get("duration_ms", 0.0) or dry.get("duration_ms", 0.0))
    auto_fixes = sum(
        1 for item in history if item.get("stage") == "fixing" and item.get("status") == "ok"
    )

    remote_ci_success = ci.get("conclusion") == "success" or (
        ci.get("status") == "completed" and not failed
    )
    dry_executed = bool(dry.get("executed", False))
    dry_success = dry_executed and int(dry.get("exit_code", 1)) == 0 and failed == 0
    no_tests_requested = not bool((state.get("options") or {}).get("run_sandbox_dry_run", True))
    validation_valid = bool(validation.get("valid", False))
    workflow_failed = (
        state.get("status") == "failed_validation"
        or not validation_valid
        or state.get("save_ok") is False
    )

    if (
        workflow_failed
        or (dry_executed and not dry_success)
        or ci.get("conclusion") in {"failure", "timed_out", "cancelled"}
    ):
        final_status = "Failed"
        badge = "❌ Failed"
        run_status = "failed"
    elif (remote_ci_success or dry_success) and auto_fixes > 0:
        final_status = "Fixed"
        badge = f"🔧 Fixed after {auto_fixes} attempt(s)"
        run_status = "completed"
    elif remote_ci_success or dry_success:
        final_status = "Passed"
        badge = "✅ Passed"
        run_status = "completed"
    elif validation_valid and no_tests_requested:
        final_status = "Validated only"
        badge = "🧪 Validated; execution disabled"
        run_status = "completed"
    elif validation_valid and not dry_executed:
        final_status = "Not Run"
        badge = "⚠️ Validated; sandbox not run"
        run_status = "completed"
    else:
        final_status = "Failed"
        badge = "❌ Failed"
        run_status = "failed"

    summary_data = {
        "total": total,
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "coverage_pct": coverage,
        "auto_fixes": auto_fixes,
        "duration_ms": duration,
        "final_status": final_status,
        "badge": badge,
    }
    try:
        summary = ResultSummarySchema.model_validate(summary_data).model_dump(mode="json")
    except Exception:
        # Keep aggregation available even if an upstream provider has a non-standard result shape.
        summary = summary_data

    test_cases = [
        {
            **case,
            "marker": case.get("marker") or ("✅" if case.get("status") == "passed" else "❌"),
        }
        for case in test_cases
    ]
    errors: List[str] = list(state.get("errors") or [])
    errors.extend(validation.get("errors") or [])
    errors.extend(ci.get("errors") or [])
    # Deduplicate while preserving the order used in the UI.
    errors = list(dict.fromkeys(str(item) for item in errors if item))

    final_result = {
        "summary": summary,
        "test_results": test_cases,
        "error_logs": errors,
        "validation_report": validation,
        "ci_result": ci,
        "history": history,
        "file_path": state.get("file_path"),
        "commit_sha": state.get("commit_sha"),
        "pr_url": state.get("pr_url"),
        "pr_comment_url": None,
        "language": state.get("language"),
        "framework": state.get("framework"),
        "syntax_highlighting": state.get("language"),
        "module_name": state.get("module_name"),
    }

    options = state.get("options") or {}
    pr_url = state.get("pr_url")
    repo_url = state.get("repo_url")
    pr_match = re.search(r"/pull/(\d+)", str(pr_url or ""))
    if options.get("create_pr") and repo_url and pr_match:
        comment_lines = [
            "## AI-Powered Test Case Generator",
            "",
            f"**Status:** {badge}",
            f"**Run:** `{state.get('run_id', '')}`",
            f"**Tests:** {passed} passed / {failed} failed / {total} total",
            f"**Coverage:** {coverage}% · **Auto-fixes:** {auto_fixes}",
            "",
            "| Result | Test | Duration (ms) |",
            "|---|---|---:|",
        ]
        comment_lines.extend(
            f"| {case.get('marker', '✅')} | `{case.get('name', 'test')}` | {case.get('duration_ms', 0)} |"
            for case in test_cases[:100]
        )
        comment_url = post_pr_summary_comment(
            str(repo_url), int(pr_match.group(1)), "\n".join(comment_lines)
        )
        if comment_url:
            final_result["pr_comment_url"] = comment_url
        else:
            comment_error = "Could not post the generated-test summary comment to the pull request."
            errors.append(comment_error)
            final_result["error_logs"] = errors

    result = {
        **state,
        "status": run_status,
        "current_stage": "completed" if run_status == "completed" else "failed",
        "progress": 100,
        "final_result": final_result,
        "errors": errors,
    }
    record_stage(
        result,
        "collecting",
        started,
        input_summary=f"dry_run={dry.get('executed')}, ci={ci.get('status')}, history={len(history)}",
        output_summary=f"{badge}; total={total}, passed={passed}, failed={failed}, auto_fixes={auto_fixes}",
        status="ok" if run_status == "completed" else "failed",
        errors=errors[-20:],
        code_snapshot=str(state.get("test_code", "")),
        current_stage="completed" if run_status == "completed" else "failed",
        progress=100,
        run_status=run_status,
        updates={"final_result": final_result},
    )
    # Include collecting event in the final result history and persist the final UI payload.
    final_result["history"] = list(result.get("history") or [])
    result["final_result"] = final_result
    try:
        update_run_stage(
            str(state.get("run_id", "")),
            stage=result["current_stage"],
            progress=100,
            status=run_status,
            final_result=final_result,
        )
    except Exception:
        logger.exception(
            "Could not persist final result payload", extra={"run_id": state.get("run_id")}
        )
    return result
