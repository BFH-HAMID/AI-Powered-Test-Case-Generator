"""EventBridge scheduled regression generation entrypoint."""

from __future__ import annotations

import re
import uuid
from typing import Any, Dict, List

from app.api.schemas import GenerateOptions, GenerateRequest
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.db.session import create_run_record, get_run_with_details, init_db
from app.tools.github_tool import fetch_github_file, normalize_repo_slug

logger = get_logger(__name__)
SUPPORTED_EXTENSIONS = {".py", ".js", ".jsx", ".ts", ".tsx", ".java"}


def _run_request(repo_url: str, branch: str, path: str, source: str) -> GenerateRequest:
    """Build a run request from one configured regression module path."""
    extension = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    if extension == "py":
        language, framework = "python", "pytest"
    elif extension in {"js", "jsx", "ts", "tsx"}:
        language, framework = "javascript", "jest"
    else:
        language, framework = "java", "junit"
    module = re.sub(r"\.(py|js|jsx|ts|tsx|java)$", "", path, flags=re.I)
    if language == "python":
        module = module.replace("/", ".")
    requirement = (
        f"Create regression tests for `{path}` in `{repo_url}` at branch `{branch}`. "
        "Cover public behavior, important boundaries, and documented errors without external network access."
    )
    return GenerateRequest(
        requirement=requirement,
        source_code=source,
        module_name=module,
        language=language,
        framework=framework,
        repo=repo_url,
        branch=branch,
        options=GenerateOptions(
            model=get_settings().llm_model,
            max_retries=get_settings().max_iterations_default,
            coverage_target=80,
            include_edge_cases=True,
            include_negative_tests=True,
            run_sandbox_dry_run=True,
            auto_commit=True,
            create_pr=True,
            trigger_ci=True,
        ),
    )


def handle_scheduled_event(event: Dict[str, Any]) -> Dict[str, Any]:
    """Fetch configured source files and run the test generator for EventBridge weekly events.

    Event shape:
      {"automation":"weekly-regression", "repo":"owner/repo", "branch":"main",
       "modules":["src/math_utils.py", "src/format.js"]}
    At most five modules are accepted per invocation to fit Lambda's maximum runtime.
    """
    configure_logging(get_settings().log_level)
    init_db()
    repo_value = str(event.get("repo") or get_settings().github_default_repo or "")
    slug = normalize_repo_slug(repo_value)
    branch = str(event.get("branch") or get_settings().github_default_branch)
    modules = event.get("modules") or []
    if not slug or not isinstance(modules, list):
        return {
            "status": "invalid_configuration",
            "message": "Configure a GitHub repo and modules array.",
        }
    repo_url = f"https://github.com/{slug}"
    created_runs: List[Dict[str, str]] = []
    skipped: List[Dict[str, str]] = []

    for raw_path in modules[:5]:
        path = str(raw_path).strip().lstrip("/")
        ext = "." + path.rsplit(".", 1)[-1].lower() if "." in path else ""
        if not path or ".." in path.split("/") or ext not in SUPPORTED_EXTENSIONS:
            skipped.append({"path": path, "reason": "unsupported or unsafe source path"})
            continue
        source = fetch_github_file(repo_url, path, branch)
        if not source:
            skipped.append({"path": path, "reason": "source file could not be fetched"})
            continue

        request = _run_request(repo_url, branch, path, source)
        run_id = uuid.uuid4().hex
        create_run_record(
            run_id=run_id,
            requirement=request.requirement,
            source_code=request.source_code,
            module_name=request.module_name,
            language=request.language,
            framework=request.framework,
            repo_url=request.repo,
            branch=request.branch,
            options=request.options.model_dump(mode="json"),
            max_iterations=request.options.max_retries,
        )
        created_runs.append({"run_id": run_id, "path": path})
        # A schedule is a one-shot Lambda invocation; run the graph synchronously so the task
        # does not disappear when the invocation returns. For larger batches use SQS/Celery.
        from app.api.routes import run_generation_task

        run_generation_task(run_id)

    results = []
    for item in created_runs:
        run = get_run_with_details(item["run_id"])
        results.append(
            {
                **item,
                "status": run.get("status") if run else "unknown",
                "summary": (run.get("final_result") or {}).get("summary") if run else None,
                "pr_url": run.get("pr_url") if run else None,
            }
        )
    return {
        "status": "completed",
        "repo": slug,
        "branch": branch,
        "created_runs": results,
        "skipped": skipped,
    }
