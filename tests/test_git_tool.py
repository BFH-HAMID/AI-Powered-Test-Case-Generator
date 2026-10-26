"""Tests for safe test-file writes and local Git branch commits."""

from __future__ import annotations

import pytest
from git import Repo

from app.tools.git_tool import (
    PathTraversalError,
    resolve_whitelisted_path,
    safe_write_test_file,
    save_and_commit_tests,
)


def test_path_whitelist_and_traversal_rejection(tmp_path) -> None:
    allowed = resolve_whitelisted_path(
        "test_calc_feature.py", base_dir=tmp_path, allowed_subdir="tests/generated"
    )
    assert allowed == (tmp_path / "tests/generated/test_calc_feature.py").resolve()
    with pytest.raises(PathTraversalError):
        resolve_whitelisted_path(
            "../outside.py", base_dir=tmp_path, allowed_subdir="tests/generated"
        )
    with pytest.raises(PathTraversalError):
        resolve_whitelisted_path(
            "/tmp/outside.py", base_dir=tmp_path, allowed_subdir="tests/generated"
        )


def test_safe_write_backs_up_existing_file(tmp_path) -> None:
    target, backup = safe_write_test_file(
        "assert True\n",
        "test_sample.py",
        base_dir=tmp_path,
        allowed_subdir="tests/generated",
    )
    assert target.read_text() == "assert True\n"
    assert backup is None
    target2, backup2 = safe_write_test_file(
        "assert 2 == 2\n",
        "test_sample.py",
        base_dir=tmp_path,
        allowed_subdir="tests/generated",
        overwrite=False,
    )
    assert target2 == target
    assert backup2 is not None and backup2.exists()
    assert backup2.read_text() == "assert True\n"
    assert target.read_text() == "assert 2 == 2\n"


def test_save_and_commit_creates_ai_tests_branch(tmp_path) -> None:
    result = save_and_commit_tests(
        run_id="run-test-123",
        test_code="def test_generated():\n    assert 1 + 1 == 2\n",
        module_name="math_tools",
        language="python",
        repo_path=tmp_path,
        allowed_subdir="tests/generated",
        auto_commit=True,
        overwrite=True,
    )
    assert result.file_path.endswith("test_math_tools_feature.py")
    assert result.branch_name == "ai-tests/run-test-123"
    assert result.commit_sha
    repo = Repo(tmp_path)
    assert repo.active_branch.name == result.branch_name
    assert repo.head.commit.hexsha == result.commit_sha
    assert (tmp_path / "tests/generated/test_math_tools_feature.py").exists()
