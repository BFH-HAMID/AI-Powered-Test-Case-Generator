"""GitHub integration using PyGithub and httpx: PR creation, Actions CI polling, JUnit XML parsing, and PR comments."""

from __future__ import annotations

import hashlib
import hmac
import io
import re
import time
import zipfile
from pathlib import PurePosixPath
from typing import Any, Dict, Optional
from urllib.parse import quote

import httpx
from github import Auth, Github

from app.core.config import get_settings
from app.core.logging import get_logger
from app.tools.sandbox_runner import parse_coverage_xml, parse_junit_xml

logger = get_logger(__name__)


def normalize_repo_slug(repo_input: Optional[str]) -> Optional[str]:
    """Normalize a GitHub URL or 'owner/repo' string into 'owner/repo'."""
    if not repo_input or not repo_input.strip():
        return None
    cleaned = repo_input.strip().rstrip("/")
    if cleaned.endswith(".git"):
        cleaned = cleaned[:-4]
    match = re.search(r"github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/]+)$", cleaned)
    if match:
        return f"{match.group('owner')}/{match.group('repo')}"
    if re.match(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", cleaned):
        return cleaned
    return None


def verify_github_webhook_signature(
    payload_body: bytes,
    signature_header: Optional[str],
    secret: Optional[str] = None,
) -> bool:
    """Verify GitHub X-Hub-Signature-256 HMAC-SHA256 header."""
    settings = get_settings()
    webhook_secret = (secret if secret is not None else settings.github_webhook_secret) or ""
    if not webhook_secret or not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = (
        "sha256="
        + hmac.new(
            webhook_secret.encode("utf-8"),
            msg=payload_body,
            digestmod=hashlib.sha256,
        ).hexdigest()
    )
    return hmac.compare_digest(expected, signature_header.strip())


def fetch_github_file(
    repo_url: str, file_path: str, ref: str, token: Optional[str] = None
) -> Optional[str]:
    """Fetch a text source file from a GitHub repository at a commit/ref via the Contents API."""
    settings = get_settings()
    slug = normalize_repo_slug(repo_url)
    if not slug or not file_path or file_path.startswith("/") or ".." in file_path.split("/"):
        return None
    headers = {"Accept": "application/vnd.github+json"}
    gh_token = token or settings.github_token
    if gh_token:
        headers["Authorization"] = f"Bearer {gh_token}"
    try:
        with httpx.Client(headers=headers, timeout=15.0) as client:
            response = client.get(
                f"https://api.github.com/repos/{slug}/contents/{quote(file_path, safe='/')}",
                params={"ref": ref},
            )
            if response.status_code != 200:
                return None
            data = response.json()
            if data.get("encoding") == "base64" and data.get("content"):
                import base64

                return base64.b64decode(data["content"]).decode("utf-8", errors="replace")
            download_url = data.get("download_url")
            if download_url and len(data.get("content", "")) < 1:
                raw_response = client.get(download_url)
                if raw_response.status_code == 200:
                    return raw_response.text
    except Exception as exc:
        logger.warning(f"Failed to fetch {file_path} from {slug}@{ref}: {exc}")
    return None


def commit_file_to_github(
    repo_url: str,
    file_path: str,
    content: str,
    run_id: str,
    base_branch: str = "main",
    commit_message: Optional[str] = None,
    token: Optional[str] = None,
    create_pr: bool = False,
) -> Optional[Dict[str, Any]]:
    """Create an ai-tests/<run_id> GitHub branch, commit a whitelisted test file, and optionally open a PR.

    The supplied path must remain inside ALLOWED_TEST_DIR. This API-based path is useful for
    Lambda deployments where a durable local Git checkout is unavailable.
    """
    settings = get_settings()
    gh_token = token or settings.github_token
    slug = normalize_repo_slug(repo_url)
    allowed_dirs = {
        PurePosixPath(settings.allowed_test_dir.strip("/")),
        PurePosixPath("src/test/java"),
    }
    normalized = PurePosixPath(file_path)
    if not gh_token or not slug:
        return None
    if (
        normalized.is_absolute()
        or ".." in normalized.parts
        or not any(normalized.is_relative_to(allowed) for allowed in allowed_dirs)
    ):
        allowed_text = " or ".join(sorted(str(path) for path in allowed_dirs))
        raise ValueError(
            f"GitHub file path must be inside an allowed tests directory ({allowed_text})"
        )

    try:
        gh = Github(auth=Auth.Token(gh_token), timeout=20)
        repo = gh.get_repo(slug)
        base = base_branch or settings.github_default_branch
        base_sha = repo.get_branch(base).commit.sha
        branch_name = f"ai-tests/{re.sub(r'[^A-Za-z0-9_-]', '-', run_id)[:40]}"
        try:
            repo.create_git_ref(ref=f"refs/heads/{branch_name}", sha=base_sha)
        except Exception:
            # Reuse an existing run branch for idempotent retries.
            repo.get_branch(branch_name)

        message = commit_message or f"test: add AI-generated tests for run {run_id}"
        try:
            existing = repo.get_contents(str(normalized), ref=branch_name)
            commit = repo.update_file(
                path=str(normalized),
                message=message,
                content=content,
                sha=existing.sha,
                branch=branch_name,
            )["commit"]
        except Exception:
            commit = repo.create_file(
                path=str(normalized),
                message=message,
                content=content,
                branch=branch_name,
            )["commit"]

        pr_url = None
        if create_pr:
            open_pr = next(
                (
                    pr
                    for pr in repo.get_pulls(state="open", base=base)
                    if pr.head.ref == branch_name
                ),
                None,
            )
            if open_pr:
                pr_url = open_pr.html_url
            else:
                pr = repo.create_pull(
                    title=f"test: AI-generated tests for {run_id}",
                    body=(
                        f"Generated test suite for run `{run_id}`. The agent completed static validation; "
                        "review the test file and CI results before merging."
                    ),
                    head=branch_name,
                    base=base,
                )
                pr_url = pr.html_url
        return {
            "branch_name": branch_name,
            "commit_sha": commit.sha,
            "pushed": True,
            "pr_url": pr_url,
            "file_path": str(normalized),
            "html_url": f"https://github.com/{slug}/tree/{branch_name}",
        }
    except Exception as exc:
        logger.warning(f"GitHub file commit failed for {slug}: {exc}")
        return None


def create_pull_request_for_run(
    repo_url: str,
    head_branch: str,
    base_branch: str = "main",
    title: str = "chore(tests): add AI-generated test suite",
    body: str = "Automated test cases generated and validated by AI-Powered Test Case Generator.",
    token: Optional[str] = None,
) -> Optional[str]:
    """Open a Pull Request on GitHub using PyGithub and return its HTML URL."""
    settings = get_settings()
    gh_token = token or settings.github_token
    slug = normalize_repo_slug(repo_url or settings.github_default_repo)
    if not gh_token or not slug:
        return None
    try:
        gh = Github(auth=Auth.Token(gh_token), timeout=15)
        repo = gh.get_repo(slug)
        pr = repo.create_pull(
            title=title,
            body=body,
            head=head_branch,
            base=base_branch or "main",
        )
        return pr.html_url
    except Exception as exc:
        logger.warning(f"Failed to create GitHub PR for {slug} ({head_branch}): {exc}")
        return None


def post_pr_summary_comment(
    repo_url: str,
    pr_number: int,
    summary_markdown: str,
    token: Optional[str] = None,
) -> Optional[str]:
    """Post a formatted Markdown test summary comment on a GitHub Pull Request (Step 5)."""
    settings = get_settings()
    gh_token = token or settings.github_token
    slug = normalize_repo_slug(repo_url or settings.github_default_repo)
    if not gh_token or not slug or pr_number <= 0:
        return None
    try:
        gh = Github(auth=Auth.Token(gh_token), timeout=15)
        repo = gh.get_repo(slug)
        pr = repo.get_pull(pr_number)
        comment = pr.create_issue_comment(summary_markdown)
        return comment.html_url
    except Exception as exc:
        logger.warning(f"Failed to post PR comment on {slug}#{pr_number}: {exc}")
        return None


def _extract_xml_from_artifact_zip(zip_bytes: bytes) -> tuple[str, str]:
    """Extract junit.xml and coverage.xml content from a GitHub Actions artifact zip."""
    junit_xml = ""
    coverage_xml = ""
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            for name in zf.namelist():
                lower = name.lower()
                if lower.endswith("junit.xml") or "junit" in lower:
                    junit_xml = zf.read(name).decode("utf-8", errors="replace")
                elif lower.endswith("coverage.xml"):
                    coverage_xml = zf.read(name).decode("utf-8", errors="replace")
    except Exception as exc:
        logger.warning(f"Failed to unpack artifact zip: {exc}")
    return junit_xml, coverage_xml


def poll_github_actions_run(
    repo_url: Optional[str],
    commit_sha: Optional[str],
    token: Optional[str] = None,
    timeout_seconds: int = 30,
    poll_interval: float = 2.0,
) -> Optional[Dict[str, Any]]:
    """Find the GitHub Actions workflow run for commit_sha, poll until complete, and parse JUnit XML."""
    settings = get_settings()
    gh_token = token or settings.github_token
    slug = normalize_repo_slug(repo_url or settings.github_default_repo)
    if not gh_token or not slug or not commit_sha:
        return None

    headers = {
        "Authorization": f"Bearer {gh_token}",
        "Accept": "application/vnd.github+json",
    }
    deadline = time.monotonic() + timeout_seconds

    try:
        with httpx.Client(headers=headers, timeout=10.0) as client:
            target_run: Optional[Dict[str, Any]] = None
            while time.monotonic() < deadline:
                resp = client.get(
                    f"https://api.github.com/repos/{slug}/actions/runs",
                    params={"head_sha": commit_sha, "per_page": 5},
                )
                if resp.status_code != 200:
                    break
                runs = resp.json().get("workflow_runs", [])
                if runs:
                    target_run = runs[0]
                    if target_run.get("status") == "completed":
                        break
                time.sleep(poll_interval)

            if not target_run:
                return None

            run_id = target_run.get("id")
            conclusion = target_run.get("conclusion") or "neutral"
            html_url = target_run.get("html_url")

            junit_xml = ""
            coverage_xml = ""
            art_resp = client.get(
                f"https://api.github.com/repos/{slug}/actions/runs/{run_id}/artifacts"
            )
            if art_resp.status_code == 200:
                for art in art_resp.json().get("artifacts", []):
                    dl_url = art.get("archive_download_url")
                    if dl_url:
                        zip_resp = client.get(dl_url, follow_redirects=True)
                        if zip_resp.status_code == 200:
                            j_xml, c_xml = _extract_xml_from_artifact_zip(zip_resp.content)
                            junit_xml = junit_xml or j_xml
                            coverage_xml = coverage_xml or c_xml

            total, passed, failed, skipped, dur_ms, cases = parse_junit_xml(junit_xml)
            cov_pct = parse_coverage_xml(coverage_xml)

            return {
                "provider": "github-actions",
                "workflow_run_id": run_id,
                "status": target_run.get("status", "completed"),
                "conclusion": conclusion,
                "html_url": html_url,
                "commit_sha": commit_sha,
                "total": total,
                "passed": passed,
                "failed": failed,
                "skipped": skipped,
                "duration_ms": dur_ms,
                "coverage_pct": cov_pct,
                "test_cases": [c.model_dump() for c in cases],
                "errors": [
                    c.message for c in cases if c.status in ("failed", "error") and c.message
                ],
            }
    except Exception as exc:
        logger.warning(f"GitHub Actions polling fallback triggered: {exc}")
        return None
