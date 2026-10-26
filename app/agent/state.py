"""Typed state contract shared by all LangGraph nodes."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, TypedDict


class AgentState(TypedDict, total=False):
    """Data carried through the parse → generate → validate → save → CI workflow."""

    run_id: str
    requirement: str
    source_code: Optional[str]
    module_name: str
    language: str
    framework: str
    repo_url: Optional[str]
    branch: Optional[str]
    options: Dict[str, Any]
    parsed_spec: Dict[str, Any]
    code_analysis: Dict[str, Any]
    test_code: str
    original_test_code: str
    validation_report: Dict[str, Any]
    iteration: int
    max_iterations: int
    history: List[Dict[str, Any]]
    file_path: Optional[str]
    commit_sha: Optional[str]
    pr_url: Optional[str]
    ci_result: Dict[str, Any]
    final_result: Dict[str, Any]
    errors: List[str]
    status: str
    current_stage: str
    progress: int
    should_run_ci: bool
    auto_commit: bool
    create_pr: bool
    run_sandbox_dry_run: bool
    graph_error: Optional[str]
    repair_made: bool
    save_ok: bool
    pushed: bool
    git_branch: Optional[str]
