"""File system whitelist enforcement and GitPython integration (branch, commit, push)."""

from __future__ import annotations

import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from git import Actor, Repo

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class PathTraversalError(PermissionError):
    """Raised when a file write attempts to escape the whitelisted test directory."""


@dataclass
class SaveAndCommitResult:
    """Result of saving generated test file and committing via GitPython."""

    file_path: str
    relative_path: str
    backup_path: Optional[str] = None
    branch_name: Optional[str] = None
    commit_sha: Optional[str] = None
    pushed: bool = False
    pr_url: Optional[str] = None
    message: str = ""


def sanitize_module_slug(module_name: str) -> str:
    """Sanitize a module name into a safe filename slug."""
    cleaned = re.sub(r"[^a-zA-Z0-9_]", "_", (module_name or "module").strip())
    cleaned = re.sub(r"_+", "_", cleaned).strip("_").lower()
    return cleaned or "module"


def java_package_path(source_code: Optional[str], module_name: str = "") -> str:
    """Extract a Java package path from source or a fully qualified module hint."""
    if source_code:
        match = re.search(r"^\s*package\s+([A-Za-z_][A-Za-z0-9_.]*)\s*;", source_code, re.MULTILINE)
        if match:
            return match.group(1).replace(".", "/")
    candidate = (module_name or "").strip().strip(".")
    if "." in candidate:
        return "/".join(candidate.split(".")[:-1])
    return ""


def build_conventional_test_filename(
    module_name: str,
    language: str = "python",
    feature_slug: str = "feature",
) -> str:
    """Return a conventional test filename for the given language and module."""
    slug = sanitize_module_slug(module_name)
    feat = sanitize_module_slug(feature_slug)
    lang = (language or "python").strip().lower()
    if lang in ("javascript", "js"):
        return f"test_{slug}_{feat}.test.js"
    if lang == "java":
        pascal = "".join(word.capitalize() for word in slug.split("_")) or "Module"
        return f"{pascal}Test.java"
    return f"test_{slug}_{feat}.py"


def resolve_whitelisted_path(
    target_filename: str,
    base_dir: Optional[Path] = None,
    allowed_subdir: Optional[str] = None,
) -> Path:
    """Resolve target_filename inside the whitelisted directory and prevent path traversal.

    Raises PathTraversalError if the path contains '..' or resolves outside the whitelist.
    """
    settings = get_settings()
    root = (base_dir or settings.project_root).resolve()
    sub = (allowed_subdir or settings.allowed_test_dir).strip("/")

    if not target_filename or not target_filename.strip():
        raise PathTraversalError("Target filename must not be empty")

    raw = target_filename.strip()
    if ".." in raw or raw.startswith("/") or raw.startswith("\\") or ":" in raw:
        raise PathTraversalError(
            f"Rejected unsafe path '{target_filename}': path traversal or absolute path is forbidden"
        )

    whitelist_root = (root / sub).resolve()
    whitelist_root.mkdir(parents=True, exist_ok=True)

    # Allow passing either bare filename ("test_calc_feature.py") or prefixed with allowed_subdir
    if raw.startswith(sub + "/"):
        candidate = (root / raw).resolve()
    else:
        candidate = (whitelist_root / raw).resolve()

    if not candidate.is_relative_to(whitelist_root):
        raise PathTraversalError(
            f"Path '{candidate}' escapes whitelisted directory '{whitelist_root}'"
        )

    return candidate


def safe_write_test_file(
    code: str,
    target_filename: str,
    base_dir: Optional[Path] = None,
    allowed_subdir: Optional[str] = None,
    overwrite: bool = False,
) -> tuple[Path, Optional[Path]]:
    """Write test code into the whitelisted directory, backing up existing files if overwrite=False."""
    dest_path = resolve_whitelisted_path(
        target_filename=target_filename,
        base_dir=base_dir,
        allowed_subdir=allowed_subdir,
    )
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    backup_path: Optional[Path] = None
    if dest_path.exists() and not overwrite:
        ts = int(time.time())
        backup_path = dest_path.with_suffix(f"{dest_path.suffix}.bak.{ts}")
        shutil.copy2(dest_path, backup_path)

    dest_path.write_text(code, encoding="utf-8")
    return dest_path, backup_path


def _ensure_git_repo_initialized(repo_dir: Path) -> Repo:
    """Initialize a Git repository with an initial commit if one does not already exist."""
    repo_dir.mkdir(parents=True, exist_ok=True)
    git_meta = repo_dir / ".git"
    if git_meta.exists():
        repo = Repo(str(repo_dir))
    else:
        repo = Repo.init(str(repo_dir))

    author = Actor("AI Test Generator Bot", "ai-test-bot@example.com")
    if not repo.heads:
        readme = repo_dir / "README.md"
        if not readme.exists():
            readme.write_text("# AI Test Generator Workspace\n", encoding="utf-8")
        repo.index.add([str(readme.relative_to(repo_dir))])
        repo.index.commit("Initial commit", author=author, committer=author)
    return repo


def save_and_commit_tests(
    run_id: str,
    test_code: str,
    module_name: str = "target_module",
    language: str = "python",
    feature_slug: str = "feature",
    target_filename: Optional[str] = None,
    repo_path: Optional[Path] = None,
    allowed_subdir: Optional[str] = None,
    overwrite: bool = False,
    auto_commit: bool = True,
    push: bool = False,
    commit_message: Optional[str] = None,
    base_branch: Optional[str] = None,
) -> SaveAndCommitResult:
    """Save generated test file to whitelisted directory and commit to branch ai-tests/<run_id>."""
    settings = get_settings()
    filename = target_filename or build_conventional_test_filename(
        module_name=module_name, language=language, feature_slug=feature_slug
    )

    # Determine target base directory
    if repo_path is not None:
        effective_base = Path(repo_path).resolve()
        git_workspace = effective_base
    elif settings.git_repo_path:
        effective_base = Path(settings.git_repo_path).resolve()
        git_workspace = effective_base
    else:
        effective_base = settings.project_root.resolve()
        # By default the application's current repository is the Git target. Set
        # GIT_ISOLATED_WORKSPACE explicitly to write/commit into a separate local workspace.
        git_workspace = (
            Path(settings.git_isolated_workspace).resolve()
            if settings.git_isolated_workspace
            else effective_base
        )

    dest_path, backup_path = safe_write_test_file(
        code=test_code,
        target_filename=filename,
        base_dir=effective_base,
        allowed_subdir=allowed_subdir,
        overwrite=overwrite,
    )
    rel_path = str(dest_path.relative_to(effective_base))

    branch_name: Optional[str] = None
    commit_sha: Optional[str] = None
    pushed = False

    if auto_commit:
        repo = _ensure_git_repo_initialized(git_workspace)
        if git_workspace != effective_base:
            # Mirror file into the isolated git workspace under the same whitelisted relative path
            safe_write_test_file(
                code=test_code,
                target_filename=filename,
                base_dir=git_workspace,
                allowed_subdir=allowed_subdir,
                overwrite=True,
            )

        clean_run_id = re.sub(r"[^a-zA-Z0-9_-]", "-", run_id)[:40]
        branch_name = f"ai-tests/{clean_run_id}"
        author = Actor("AI Test Generator", "ai-test-bot@example.com")

        branch_names = [head.name for head in repo.heads]
        if branch_name in branch_names:
            target_head = repo.heads[branch_name]
        else:
            if base_branch and base_branch in branch_names:
                repo.heads[base_branch].checkout()
            target_head = repo.create_head(branch_name)
        target_head.checkout()

        # Force-add because the default project .gitignore excludes generated artifacts until a user commits them.
        repo.git.add("-f", "--", rel_path)
        msg = commit_message or (
            f"test({sanitize_module_slug(module_name)}): add AI-generated {language} tests "
            f"[run {clean_run_id}]"
        )
        commit_obj = repo.index.commit(msg, author=author, committer=author)
        commit_sha = commit_obj.hexsha

        if push and "origin" in [r.name for r in repo.remotes]:
            try:
                repo.remotes.origin.push(refspec=f"{branch_name}:{branch_name}")
                pushed = True
            except Exception as exc:
                logger.warning(f"Git push to origin skipped/failed: {exc}")

    return SaveAndCommitResult(
        file_path=str(dest_path),
        relative_path=rel_path,
        backup_path=str(backup_path) if backup_path else None,
        branch_name=branch_name,
        commit_sha=commit_sha,
        pushed=pushed,
        message=f"Saved to {rel_path}" + (f" on branch {branch_name}" if branch_name else ""),
    )
