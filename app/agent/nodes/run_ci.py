"""GitHub Actions integration node: poll workflow runs and retrieve parsed test results."""

from __future__ import annotations

import time
from typing import Any, Dict, List

from app.agent.nodes._common import record_stage
from app.core.config import get_settings
from app.core.logging import get_logger
from app.tools.github_tool import poll_github_actions_run

logger = get_logger(__name__)


def run_ci_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """LangGraph node: poll GitHub Actions only when a branch was pushed to a configured repo."""
    started = time.perf_counter()
    options = state.get("options") or {}
    repo_url = state.get("repo_url") or get_settings().github_default_repo
    commit_sha = state.get("commit_sha")
    pushed = bool(state.get("pushed", False))
    errors: List[str] = []
    ci_result: Dict[str, Any] = {
        "provider": "github-actions",
        "status": "skipped",
        "conclusion": "neutral",
        "reason": "CI polling requires trigger_ci=true, a pushed commit SHA, repository URL, and GITHUB_TOKEN.",
        "test_cases": [],
        "errors": [],
    }

    if bool(options.get("trigger_ci", True)):
        if not repo_url:
            ci_result["reason"] = (
                "No GitHub repository was supplied; local sandbox results are used."
            )
        elif not commit_sha:
            ci_result["reason"] = "No Git commit SHA was produced; local sandbox results are used."
        elif not pushed:
            ci_result["reason"] = (
                "Commit was not pushed; GitHub Actions cannot observe it. Local sandbox results are used."
            )
        elif not get_settings().github_token:
            ci_result["reason"] = (
                "GITHUB_TOKEN is not configured; GitHub Actions polling was skipped."
            )
        else:
            try:
                remote = poll_github_actions_run(
                    repo_url=str(repo_url),
                    commit_sha=str(commit_sha),
                    timeout_seconds=int(options.get("ci_timeout_seconds", 120)),
                )
                if remote:
                    ci_result = remote
                else:
                    ci_result["status"] = "unavailable"
                    ci_result["reason"] = (
                        "No completed workflow run or JUnit artifact was available before timeout."
                    )
            except Exception as exc:
                ci_result["status"] = "error"
                ci_result["reason"] = f"GitHub Actions polling failed: {exc}"
                errors.append(ci_result["reason"])
                logger.exception(
                    "GitHub Actions polling failed", extra={"run_id": state.get("run_id")}
                )

    result = {
        **state,
        "ci_result": ci_result,
        "errors": list(state.get("errors") or []) + errors,
    }
    record_stage(
        result,
        "ci_running",
        started,
        input_summary=f"repo={repo_url or 'none'}, commit={commit_sha or 'none'}, pushed={pushed}",
        output_summary=f"CI status={ci_result.get('status')}; conclusion={ci_result.get('conclusion')}",
        status="ok" if ci_result.get("status") not in {"error"} else "failed",
        errors=errors,
        current_stage="ci_running",
        progress=88,
        run_status="running",
        updates={"ci_result": ci_result},
    )
    return result
