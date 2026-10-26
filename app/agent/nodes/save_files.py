"""File system, S3 artifact, GitPython, and optional GitHub API integration node."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.agent.nodes._common import record_stage
from app.core.config import get_settings
from app.core.logging import get_logger
from app.tools.artifact_store import upload_run_artifacts
from app.tools.git_tool import (
    SaveAndCommitResult,
    build_conventional_test_filename,
    java_package_path,
    save_and_commit_tests,
)
from app.tools.github_tool import commit_file_to_github, normalize_repo_slug

logger = get_logger(__name__)


def save_files_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """LangGraph node: safely persist tests, create a branch/commit, and optionally open a PR.

    On Lambda, local application code is read-only and ephemeral: tests are sent to S3 and,
    when configured, committed to GitHub with the Contents API. Local development uses the
    tests/ whitelist and GitPython branch/commit workflow.
    """
    started = time.perf_counter()
    run_id = str(state.get("run_id", ""))
    module_name = str(state.get("module_name") or "target_module")
    language = str(state.get("language") or "python")
    code = str(state.get("test_code", ""))
    options = state.get("options") or {}
    auto_commit = bool(options.get("auto_commit", True))
    create_pr = bool(options.get("create_pr", False))
    repo_url = state.get("repo_url")
    settings = get_settings()
    in_lambda = bool(os.getenv("AWS_LAMBDA_FUNCTION_NAME"))
    errors: List[str] = []

    file_path: Optional[str] = None
    commit_sha: Optional[str] = None
    branch_name: Optional[str] = None
    pushed = False
    pr_url: Optional[str] = None
    save_ok = False

    filename = build_conventional_test_filename(module_name, language, "feature")
    allowed_subdir = settings.allowed_test_dir
    if language == "java":
        allowed_subdir = "src/test/java"
        package_path = java_package_path(str(state.get("source_code") or ""), module_name)
        if package_path:
            filename = f"{package_path}/{filename}"
    allowed_path = f"{allowed_subdir.strip('/')}/{filename}"
    remote_requested = bool(repo_url and settings.github_token and auto_commit)
    remote_result: Optional[Dict[str, Any]] = None

    if remote_requested:
        try:
            remote_result = commit_file_to_github(
                repo_url=str(repo_url),
                file_path=allowed_path,
                content=code,
                run_id=run_id,
                base_branch=str(state.get("branch") or settings.github_default_branch),
                token=settings.github_token,
                create_pr=create_pr,
            )
            if remote_result:
                file_path = str(remote_result.get("file_path") or allowed_path)
                branch_name = remote_result.get("branch_name")
                commit_sha = remote_result.get("commit_sha")
                pushed = bool(remote_result.get("pushed"))
                pr_url = remote_result.get("pr_url")
                save_ok = True
            else:
                errors.append("GitHub API commit failed; falling back to a local whitelisted save.")
        except Exception as exc:
            logger.exception(
                "GitHub API commit failed", extra={"run_id": run_id, "stage": "saving"}
            )
            errors.append(f"GitHub API commit failed: {exc}")

    if in_lambda:
        try:
            uris = upload_run_artifacts(
                run_id,
                code,
                {
                    "run_id": run_id,
                    "module_name": module_name,
                    "language": language,
                    "framework": state.get("framework"),
                    "requirement": state.get("requirement"),
                    "validation_report": state.get("validation_report"),
                    "file_path": file_path,
                    "commit_sha": commit_sha,
                },
            )
            artifact_uri = uris.get("test_code")
            if artifact_uri and not file_path:
                file_path = artifact_uri
                save_ok = True
        except Exception as exc:
            errors.append(f"S3 artifact upload failed: {exc}")

        if not remote_result and not file_path:
            errors.append(
                "Lambda has no durable output destination. Configure ARTIFACT_BUCKET or provide a GitHub repo and GITHUB_TOKEN."
            )
    elif not remote_result:
        try:
            repo_path_value = settings.git_repo_path
            repo_path = Path(repo_path_value).resolve() if repo_path_value else None
            local_result: SaveAndCommitResult = save_and_commit_tests(
                run_id=run_id,
                test_code=code,
                module_name=module_name,
                language=language,
                feature_slug="feature",
                target_filename=filename,
                repo_path=repo_path,
                allowed_subdir=allowed_subdir,
                auto_commit=auto_commit,
                push=False,
                overwrite=False,
                base_branch=str(state.get("branch") or settings.github_default_branch),
            )
            file_path = local_result.file_path
            commit_sha = local_result.commit_sha
            branch_name = local_result.branch_name
            pushed = local_result.pushed
            save_ok = True
        except Exception as exc:
            logger.exception(
                "Failed to save generated tests", extra={"run_id": run_id, "stage": "saving"}
            )
            errors.append(f"File/Git save failed: {exc}")

    if create_pr and repo_url and not pr_url:
        if not normalize_repo_slug(str(repo_url)):
            errors.append("PR creation skipped: repo must be a GitHub URL or owner/repo slug.")
        elif not settings.github_token:
            errors.append(
                "PR creation skipped: configure GITHUB_TOKEN with repo contents and PR permissions."
            )
        elif not pushed:
            errors.append(
                "PR creation skipped: the generated branch was not pushed to GitHub. Configure a GitHub token or an authenticated repository."
            )

    result = {
        **state,
        "file_path": file_path,
        "commit_sha": commit_sha,
        "pr_url": pr_url,
        "git_branch": branch_name,
        "pushed": pushed,
        "save_ok": save_ok,
        "errors": list(state.get("errors") or []) + errors,
    }
    record_stage(
        result,
        "saving",
        started,
        input_summary=f"Save {len(code)} chars; auto_commit={auto_commit}; create_pr={create_pr}; lambda={in_lambda}",
        output_summary=(
            f"Saved {file_path}; commit={commit_sha or 'none'}; PR={pr_url or 'not created'}"
            if save_ok
            else "File save failed"
        ),
        status="ok" if save_ok else "failed",
        errors=errors,
        code_snapshot=code,
        current_stage="saving",
        progress=75,
        run_status="running" if save_ok else "failed",
        updates={"file_path": file_path, "commit_sha": commit_sha, "pr_url": pr_url},
    )
    return result
