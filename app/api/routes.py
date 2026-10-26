"""FastAPI route handlers for generation, run status/results, GitHub webhooks, feedback, and exports."""

from __future__ import annotations

import hmac
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from fastapi.responses import Response as BinaryResponse

from app.agent.graph import invoke_agent
from app.agent.nodes.validate import validate_tests_node
from app.api.schemas import (
    MAX_SOURCE_CODE_CHARS,
    CommitRequest,
    FeedbackRequest,
    GenerateRequest,
    GenerateResponse,
    PRCommentRequest,
    ResultResponse,
    ResultSummarySchema,
    RevalidateRequest,
    StatusResponse,
)
from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.session import (
    append_iteration_record,
    create_run_record,
    get_run_with_details,
    list_runs,
    save_feedback_record,
    update_run_stage,
)
from app.tools.git_tool import (
    build_conventional_test_filename,
    java_package_path,
    save_and_commit_tests,
)
from app.tools.github_tool import (
    commit_file_to_github,
    fetch_github_file,
    post_pr_summary_comment,
    verify_github_webhook_signature,
)
from app.tools.linter import check_security, check_syntax
from app.tools.report_exporter import (
    generate_markdown_report,
    generate_pdf_report,
    generate_zip_bundle,
)

logger = get_logger(__name__)

router = APIRouter()


def require_api_key(x_api_key: Optional[str] = Header(default=None, alias="X-API-Key")) -> None:
    """Enforce API-key authentication when API_KEY is configured."""
    settings = get_settings()
    expected = settings.api_key
    if not expected:
        if settings.app_env.lower() == "production":
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="API authentication is not configured.",
            )
        return
    if not x_api_key or not hmac.compare_digest(x_api_key, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="A valid X-API-Key header is required.",
        )


protected_router = APIRouter(dependencies=[Depends(require_api_key)])


def run_generation_task(run_id: str) -> None:
    """Background task entrypoint: load persisted run, invoke LangGraph, and record failures."""
    details = get_run_with_details(run_id)
    if details is None:
        logger.error(
            "Background generation run disappeared before execution", extra={"run_id": run_id}
        )
        return

    options = details.get("options") or {}
    max_iterations = int(options.get("max_retries", details.get("max_iterations", 3)))
    initial_state: Dict[str, Any] = {
        "run_id": run_id,
        "requirement": details["requirement"],
        "source_code": details.get("source_code"),
        "module_name": details.get("module_name") or "target_module",
        "language": details.get("language") or "python",
        "framework": details.get("framework") or "pytest",
        "repo_url": details.get("repo_url"),
        "branch": details.get("branch") or "main",
        "options": options,
        "parsed_spec": {},
        "code_analysis": {},
        "test_code": "",
        "original_test_code": "",
        "validation_report": {},
        "iteration": 0,
        "max_iterations": max_iterations,
        "history": [],
        "file_path": None,
        "commit_sha": None,
        "pr_url": None,
        "ci_result": {},
        "final_result": {},
        "errors": [],
        "status": "running",
        "current_stage": "parsing",
        "progress": 5,
        "should_run_ci": bool(options.get("trigger_ci", True)),
        "auto_commit": bool(options.get("auto_commit", True)),
        "create_pr": bool(options.get("create_pr", False)),
        "run_sandbox_dry_run": bool(options.get("run_sandbox_dry_run", True)),
        "repair_made": True,
        "save_ok": None,
        "pushed": False,
        "git_branch": None,
    }
    try:
        update_run_stage(run_id, "parsing", 5, status="running")
        final_state = invoke_agent(initial_state)
        if not final_state.get("final_result"):
            # Graph-level exception handlers should have returned a state, but ensure a durable failure result.
            err = final_state.get("graph_error") or "Agent graph ended without collecting results."
            update_run_stage(
                run_id,
                stage="failed",
                progress=100,
                status="failed",
                error_message=str(err),
                test_code=final_state.get("test_code"),
            )
    except Exception as exc:
        error = f"Generation workflow failed: {type(exc).__name__}: {exc}"
        logger.exception("Unhandled generation task failure", extra={"run_id": run_id})
        append_iteration_record(
            run_id=run_id,
            iteration_number=0,
            stage="failed",
            status="failed",
            input_summary="background generation task",
            output_summary=error,
            duration_ms=0.0,
            errors=[error],
        )
        update_run_stage(
            run_id,
            stage="failed",
            progress=100,
            status="failed",
            error_message=error,
            final_result={
                "summary": {
                    "total": 0,
                    "passed": 0,
                    "failed": 0,
                    "skipped": 0,
                    "coverage_pct": 0.0,
                    "auto_fixes": 0,
                    "duration_ms": 0.0,
                    "final_status": "Failed",
                    "badge": "❌ Failed",
                },
                "test_results": [],
                "error_logs": [error],
                "history": [],
            },
        )


def _make_run(request: GenerateRequest, run_id: Optional[str] = None) -> str:
    """Persist request data and return the generated run ID."""
    run_id = run_id or uuid.uuid4().hex
    options = request.options.model_dump(mode="json")
    create_run_record(
        run_id=run_id,
        requirement=request.requirement,
        source_code=request.source_code,
        module_name=request.module_name,
        language=request.language,
        framework=request.framework,
        repo_url=request.repo,
        branch=request.branch,
        options=options,
        max_iterations=request.options.max_retries,
    )
    return run_id


def _run_to_result(run: Dict[str, Any]) -> ResultResponse:
    """Transform a persisted run into a UI-ready ResultResponse schema."""
    final = run.get("final_result") or {}
    summary = final.get("summary") or {}
    if not summary:
        failed = run.get("status") in {"failed", "failed_validation"}
        summary = {
            "total": 0,
            "passed": 0,
            "failed": 0,
            "skipped": 0,
            "coverage_pct": 0.0,
            "auto_fixes": 0,
            "duration_ms": 0.0,
            "final_status": "Failed" if failed else "Not Run",
            "badge": "❌ Failed" if failed else "⚠️ Run in progress",
        }
    cases = final.get("test_results") or []
    validation = run.get("validation_report") or final.get("validation_report")
    ci = run.get("ci_result") or final.get("ci_result")
    parsed = run.get("parsed_spec")
    return ResultResponse(
        run_id=run["run_id"],
        status=run.get("status", "pending"),
        stage=run.get("current_stage", "queued"),
        language=run.get("language", "python"),
        framework=run.get("framework", "pytest"),
        syntax_highlighting=run.get("language", "python"),
        module_name=run.get("module_name", "target_module"),
        requirement=run.get("requirement", ""),
        source_code=run.get("source_code"),
        parsed_spec=parsed,
        test_code=run.get("test_code") or "",
        summary=ResultSummarySchema.model_validate(summary or {}),
        test_results=cases,
        validation_report=validation,
        ci_result=ci,
        error_logs=final.get("error_logs") or (run.get("error_message") or "").splitlines(),
        history=run.get("history") or final.get("history") or [],
        file_path=run.get("file_path"),
        commit_sha=run.get("commit_sha"),
        pr_url=run.get("pr_url"),
        pr_comment_url=final.get("pr_comment_url"),
        feedbacks=run.get("feedbacks") or [],
        created_at=run.get("created_at"),
        updated_at=run.get("updated_at"),
    )


@protected_router.post(
    "/generate", response_model=GenerateResponse, status_code=status.HTTP_202_ACCEPTED
)
def generate_tests(request: GenerateRequest, background_tasks: BackgroundTasks) -> GenerateResponse:
    """Start a background test generation run and return its run ID."""
    run_id = _make_run(request)
    if request.options.sync:
        run_generation_task(run_id)
        run = get_run_with_details(run_id) or {}
        return GenerateResponse(
            run_id=run_id,
            status=str(run.get("status", "failed")),
            current_stage=str(run.get("current_stage", "failed")),
            message="Synchronous generation finished.",
        )
    background_tasks.add_task(run_generation_task, run_id)
    return GenerateResponse(
        run_id=run_id,
        status="pending",
        current_stage="queued",
        message="Generation started. Poll GET /status/{run_id} for progress.",
    )


@protected_router.get("/status/{run_id}", response_model=StatusResponse)
def get_run_status(run_id: str) -> StatusResponse:
    """Return current stage, progress, partial outputs, and correction history."""
    run = get_run_with_details(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    partial = {
        "parsed_spec": run.get("parsed_spec"),
        "test_code": run.get("test_code"),
        "validation_report": run.get("validation_report"),
        "ci_result": run.get("ci_result"),
        "file_path": run.get("file_path"),
        "commit_sha": run.get("commit_sha"),
        "pr_url": run.get("pr_url"),
    }
    return StatusResponse(
        run_id=run_id,
        status=run.get("status", "pending"),
        stage=run.get("current_stage", "queued"),
        progress=run.get("progress", 0),
        iteration=run.get("iteration", 0),
        max_iterations=run.get("max_iterations", 3),
        partial_outputs=partial,
        history=run.get("history") or [],
        error_message=run.get("error_message"),
    )


@protected_router.get("/result/{run_id}", response_model=ResultResponse)
def get_run_result(run_id: str) -> ResultResponse:
    """Return complete UI-ready generation results and logs."""
    run = get_run_with_details(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return _run_to_result(run)


@protected_router.get("/history")
def get_run_history(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> Dict[str, Any]:
    """Return recent generation runs for the UI history panel."""
    return {"items": list_runs(limit=limit, offset=offset), "limit": limit, "offset": offset}


@router.post("/webhook/github", status_code=status.HTTP_202_ACCEPTED)
async def github_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_hub_signature_256: Optional[str] = Header(default=None, alias="X-Hub-Signature-256"),
    x_github_event: Optional[str] = Header(default=None, alias="X-GitHub-Event"),
) -> Dict[str, Any]:
    """Verify GitHub webhook HMAC and start generation for changed source files."""
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > 2_000_000:
        raise HTTPException(status_code=413, detail="GitHub webhook payload exceeds 2 MB")
    raw_body = await request.body()
    if len(raw_body) > 2_000_000:
        raise HTTPException(status_code=413, detail="GitHub webhook payload exceeds 2 MB")
    if not verify_github_webhook_signature(raw_body, x_hub_signature_256):
        raise HTTPException(status_code=401, detail="Invalid GitHub webhook signature")
    if x_github_event not in {"push", "ping"}:
        return {"accepted": True, "message": f"Event '{x_github_event}' ignored."}

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Webhook body must be valid JSON")
    if x_github_event == "ping":
        return {"accepted": True, "message": "GitHub webhook ping received."}

    repo = (payload.get("repository") or {}).get("full_name")
    after = payload.get("after") or ""
    commits = payload.get("commits") or []
    changed: List[str] = []
    for commit in commits:
        changed.extend(commit.get("added") or [])
        changed.extend(commit.get("modified") or [])
    source_paths = [
        p
        for p in dict.fromkeys(changed)
        if p.lower().endswith((".py", ".js", ".jsx", ".ts", ".tsx", ".java"))
        and not p.startswith(("tests/", "test/", "node_modules/", "vendor/"))
        and not p.endswith(".min.js")
    ][:5]
    if not repo or not after or not source_paths:
        return {
            "accepted": True,
            "message": "No supported source files changed in this push.",
            "created_runs": [],
        }

    repo_url = f"https://github.com/{repo}"
    created: List[str] = []
    for path in source_paths:
        source = fetch_github_file(repo_url, path, after)
        if not source:
            logger.warning("Webhook source file could not be fetched", extra={"stage": "webhook"})
            continue
        if len(source) > MAX_SOURCE_CODE_CHARS:
            logger.warning(
                "Webhook source file exceeded the source-size limit", extra={"stage": "webhook"}
            )
            continue
        ext = Path(path).suffix.lower()
        if ext == ".py":
            language, framework = "python", "pytest"
        elif ext in {".js", ".jsx", ".ts", ".tsx"}:
            language, framework = "javascript", "jest"
        else:
            language, framework = "java", "junit"
        module = re.sub(r"\.(py|js|jsx|ts|tsx|java)$", "", path, flags=re.I).replace("/", ".")
        requirement = (
            f"Generate a regression test suite for the changed source file `{path}`. "
            f"Cover its public behavior, boundary cases, and documented error behavior. "
            f"Push commit: {after}."
        )
        req = GenerateRequest(
            requirement=requirement,
            source_code=source,
            module_name=module,
            language=language,
            framework=framework,
            repo=repo_url,
            branch=(payload.get("ref") or "refs/heads/main").removeprefix("refs/heads/"),
        )
        run_id = _make_run(req)
        background_tasks.add_task(run_generation_task, run_id)
        created.append(run_id)

    return {
        "accepted": True,
        "message": f"Started {len(created)} generation run(s) for the push.",
        "created_runs": created,
    }


@protected_router.post("/revalidate/{run_id}")
def revalidate_run(run_id: str, request: RevalidateRequest) -> Dict[str, Any]:
    """Re-validate user-edited generated test code and optionally dry-run it in the sandbox."""
    run = get_run_with_details(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    code = request.test_code
    syntax = check_syntax(code, run.get("language", "python"))
    security = check_security(code, run.get("language", "python"))
    if not syntax.ok or not security.ok:
        report = {
            "valid": False,
            "syntax_ok": syntax.ok,
            "security_ok": security.ok,
            "security_issues": security.issues,
            "lint_ok": False,
            "lint_issues": [],
            "collect_ok": False,
            "collect_output": "Collection skipped because syntax/security validation failed.",
            "dry_run_ok": False,
            "dry_run_results": {"executed": False, "sandbox_backend": "not-run"},
            "errors": ([syntax.error] if syntax.error else []) + security.issues,
            "coverage": 0.0,
        }
    else:
        opts = dict(run.get("options") or {})
        opts["run_sandbox_dry_run"] = request.run_sandbox_dry_run
        validation_state = validate_tests_node(
            {
                "run_id": run_id,
                "requirement": run.get("requirement", ""),
                "source_code": run.get("source_code"),
                "module_name": run.get("module_name", "target_module"),
                "language": run.get("language", "python"),
                "framework": run.get("framework", "pytest"),
                "options": opts,
                "test_code": code,
                "iteration": int(run.get("iteration", 0)) + 1,
                "max_iterations": int(run.get("max_iterations", 3)),
                "history": run.get("history") or [],
                "errors": [],
                "status": "running",
            }
        )
        report = validation_state.get("validation_report") or {}

    next_status = "completed" if report.get("valid") else "failed_validation"
    previous_final = dict(run.get("final_result") or {})
    previous_summary = previous_final.get("summary") or {}
    dry_result = report.get("dry_run_results") or {}
    if not report.get("valid"):
        final_status, badge = "Failed", "❌ Failed"
    elif not request.run_sandbox_dry_run:
        final_status, badge = "Validated only", "🧪 Validated; execution disabled"
    elif dry_result.get("executed") and int(dry_result.get("exit_code", 1)) == 0:
        final_status, badge = (
            "Fixed" if previous_summary.get("auto_fixes", 0) else "Passed"
        ), "✅ Passed"
    elif dry_result.get("executed"):
        final_status, badge = "Failed", "❌ Failed"
    else:
        final_status, badge = "Not Run", "⚠️ Validated; sandbox not run"
    new_summary = ResultSummarySchema(
        total=int(dry_result.get("total", 0) or 0),
        passed=int(dry_result.get("passed", 0) or 0),
        failed=int(dry_result.get("failed", 0) or 0),
        skipped=int(dry_result.get("skipped", 0) or 0),
        coverage_pct=float(dry_result.get("coverage_pct", 0.0) or 0.0),
        auto_fixes=int(previous_summary.get("auto_fixes", 0) or 0),
        duration_ms=float(dry_result.get("duration_ms", 0.0) or 0.0),
        final_status=final_status,
        badge=badge,
    ).model_dump(mode="json")
    final_result = {
        **previous_final,
        "summary": new_summary,
        "test_results": dry_result.get("test_cases") or [],
        "error_logs": report.get("errors") or [],
        "validation_report": report,
        "ci_result": {
            "status": "stale",
            "reason": "Edited test code must be committed and rerun in CI to refresh remote results.",
            "test_cases": [],
        },
    }
    update_run_stage(
        run_id,
        stage="revalidated",
        progress=100,
        status=next_status,
        iteration=int(run.get("iteration", 0)) + 1,
        test_code=code,
        validation_report=report,
        final_result=final_result,
        error_message="\n".join(report.get("errors") or []) if not report.get("valid") else None,
    )
    append_iteration_record(
        run_id=run_id,
        iteration_number=int(run.get("iteration", 0)) + 1,
        stage="revalidate",
        status="ok" if report.get("valid") else "failed",
        input_summary="User edited generated code in the UI",
        output_summary="Re-validation passed" if report.get("valid") else "Re-validation failed",
        duration_ms=0.0,
        test_code_snapshot=code,
        errors=report.get("errors") or [],
    )
    refreshed = get_run_with_details(run_id)
    final_result["history"] = refreshed.get("history", []) if refreshed else []
    update_run_stage(
        run_id,
        stage="revalidated",
        progress=100,
        status=next_status,
        final_result=final_result,
    )
    return {"run_id": run_id, "status": next_status, "validation_report": report}


@protected_router.post("/commit/{run_id}")
def commit_run(run_id: str, request: CommitRequest) -> Dict[str, Any]:
    """Save/commit validated test code in the configured tests whitelist."""
    run = get_run_with_details(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    code = request.test_code if request.test_code is not None else run.get("test_code")
    if not code:
        raise HTTPException(status_code=400, detail="No generated test code is available")
    syntax = check_syntax(code, run.get("language", "python"))
    security = check_security(code, run.get("language", "python"))
    if not syntax.ok or not security.ok:
        raise HTTPException(
            status_code=422,
            detail={"syntax_error": syntax.error, "security_issues": security.issues},
        )

    settings = get_settings()
    repo_url = run.get("repo_url")
    language = run.get("language") or "python"
    module_name = run.get("module_name") or "target_module"
    allowed_subdir = settings.allowed_test_dir
    filename = build_conventional_test_filename(module_name, language, "feature")
    if language == "java":
        allowed_subdir = "src/test/java"
        package_path = java_package_path(run.get("source_code"), module_name)
        if package_path:
            filename = f"{package_path}/{filename}"

    if repo_url and settings.github_token:
        remote_path = f"{allowed_subdir.strip('/')}/{filename}"
        remote = commit_file_to_github(
            repo_url=str(repo_url),
            file_path=remote_path,
            content=code,
            run_id=run_id,
            base_branch=run.get("branch") or settings.github_default_branch,
            commit_message=request.commit_message,
            token=settings.github_token,
            create_pr=request.create_pr,
        )
        if not remote:
            raise HTTPException(
                status_code=502,
                detail="GitHub commit failed; check repo permissions and branch configuration",
            )
        update_run_stage(
            run_id,
            stage="completed",
            progress=100,
            status="completed",
            test_code=code,
            file_path=remote.get("file_path"),
            commit_sha=remote.get("commit_sha"),
            pr_url=remote.get("pr_url"),
        )
        return {
            "run_id": run_id,
            "file_path": remote.get("file_path"),
            "branch": remote.get("branch_name"),
            "commit_sha": remote.get("commit_sha"),
            "pushed": remote.get("pushed", True),
            "pr_url": remote.get("pr_url"),
        }

    if request.create_pr:
        raise HTTPException(
            status_code=400, detail="A GitHub repo and GITHUB_TOKEN are required to open a PR"
        )
    if os.getenv("AWS_LAMBDA_FUNCTION_NAME"):
        raise HTTPException(
            status_code=400,
            detail=(
                "Manual commits in Lambda require a GitHub repo and GITHUB_TOKEN; "
                "configure GitHub integration to commit this run."
            ),
        )
    try:
        saved = save_and_commit_tests(
            run_id=run_id,
            test_code=code,
            module_name=module_name,
            language=language,
            repo_path=Path(settings.git_repo_path).resolve() if settings.git_repo_path else None,
            allowed_subdir=allowed_subdir,
            target_filename=filename,
            auto_commit=True,
            commit_message=request.commit_message,
            overwrite=False,
            base_branch=run.get("branch") or settings.github_default_branch,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Unable to save/commit tests: {exc}")
    update_run_stage(
        run_id,
        stage="completed",
        progress=100,
        status="completed",
        test_code=code,
        file_path=saved.file_path,
        commit_sha=saved.commit_sha,
    )
    return {
        "run_id": run_id,
        "file_path": saved.file_path,
        "relative_path": saved.relative_path,
        "branch": saved.branch_name,
        "commit_sha": saved.commit_sha,
        "pushed": saved.pushed,
    }


@protected_router.post("/feedback/{run_id}")
def add_feedback(run_id: str, request: FeedbackRequest) -> Dict[str, Any]:
    """Record user rating and optional feedback tags for a generation run."""
    feedback = save_feedback_record(
        run_id,
        rating=request.rating,
        comment=request.comment,
        helpful_tags=request.helpful_tags,
    )
    if not feedback:
        raise HTTPException(status_code=404, detail="Run not found")
    return feedback


@protected_router.post("/comment/{run_id}")
def post_run_comment(run_id: str, request: PRCommentRequest) -> Dict[str, Any]:
    """Post the aggregated generation result as a Markdown comment on a GitHub PR."""
    run = get_run_with_details(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    if not run.get("repo_url"):
        raise HTTPException(status_code=400, detail="This run has no GitHub repository configured")
    final = run.get("final_result") or {}
    summary = final.get("summary") or {}
    rows = [
        "## AI-Powered Test Case Generator",
        "",
        f"**Status:** {summary.get('badge', run.get('status', 'unknown'))}",
        f"**Run:** `{run_id}`",
        f"**Summary:** {summary.get('passed', 0)} passed / {summary.get('failed', 0)} failed / {summary.get('total', 0)} total",
        f"**Coverage:** {summary.get('coverage_pct', 0.0)}% · **Auto-fixes:** {summary.get('auto_fixes', 0)}",
        "",
        "| Result | Test | Duration (ms) |",
        "|---|---|---:|",
    ]
    for case in (final.get("test_results") or [])[:100]:
        rows.append(
            f"| {case.get('marker', '✅')} | `{case.get('name', 'test')}` | {case.get('duration_ms', 0)} |"
        )
    rows.extend(["", "Generated test code is available in the attached PR branch for review."])
    comment_url = post_pr_summary_comment(
        repo_url=run["repo_url"],
        pr_number=request.pr_number,
        summary_markdown="\n".join(rows),
    )
    if not comment_url:
        raise HTTPException(
            status_code=502,
            detail="GitHub comment could not be posted; check GITHUB_TOKEN permissions",
        )
    final["pr_comment_url"] = comment_url
    update_run_stage(
        run_id, run.get("current_stage", "completed"), run.get("progress", 100), final_result=final
    )
    return {"run_id": run_id, "comment_url": comment_url}


@protected_router.get("/download/{run_id}/report.md")
def download_markdown(run_id: str) -> Response:
    """Download the run summary as Markdown."""
    run = get_run_with_details(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    markdown = generate_markdown_report(run)
    return Response(
        content=markdown,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="test-report-{run_id}.md"'},
    )


@protected_router.get("/download/{run_id}/report.pdf")
def download_pdf(run_id: str) -> Response:
    """Download the run summary as PDF."""
    run = get_run_with_details(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    pdf = generate_pdf_report(run)
    return BinaryResponse(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="test-report-{run_id}.pdf"'},
    )


@protected_router.get("/download/{run_id}/bundle.zip")
def download_zip(run_id: str) -> Response:
    """Download generated code, metadata, Markdown, and PDF report in a ZIP file."""
    run = get_run_with_details(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    data = generate_zip_bundle(run)
    return BinaryResponse(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="test-generator-{run_id}.zip"'},
    )


@router.get("/health")
def health() -> Dict[str, Any]:
    """Health/readiness endpoint used by AWS API Gateway, CI, and container health checks."""
    settings = get_settings()
    return {
        "status": "ok",
        "app": settings.app_name,
        "environment": settings.app_env,
        "llm_provider": settings.llm_provider,
    }


router.include_router(protected_router)
