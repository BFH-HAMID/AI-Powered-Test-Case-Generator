"""LangGraph end-to-end workflow tests with explicit local test settings."""

from __future__ import annotations

import asyncio

from app.agent.graph import build_agent_graph
from app.core.config import get_settings
from app.db.session import create_run_record, init_db, reset_db_engine


def test_graph_compiles_and_runs_to_results(tmp_path, monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'graph.db'}")
    monkeypatch.setattr(settings, "project_root", tmp_path)
    monkeypatch.setattr(settings, "sandbox_mode", "docker")
    reset_db_engine()
    init_db()
    create_run_record(
        run_id="graph-run-001",
        requirement="add(a, b) returns the sum.",
        source_code="def add(a: int, b: int) -> int:\n    return a + b\n",
        module_name="calc",
        language="python",
        framework="pytest",
        repo_url=None,
        branch="main",
        options={
            "max_retries": 0,
            "auto_commit": False,
            "trigger_ci": False,
            "run_sandbox_dry_run": False,
        },
        max_iterations=0,
    )
    graph = build_agent_graph()
    state = {
        "run_id": "graph-run-001",
        "requirement": "add(a, b) returns the sum.",
        "source_code": "def add(a: int, b: int) -> int:\n    return a + b\n",
        "module_name": "calc",
        "language": "python",
        "framework": "pytest",
        "repo_url": None,
        "branch": "main",
        "options": {
            "max_retries": 0,
            "auto_commit": False,
            "trigger_ci": False,
            "run_sandbox_dry_run": False,
        },
        "parsed_spec": {},
        "code_analysis": {},
        "test_code": "",
        "original_test_code": "",
        "validation_report": {},
        "iteration": 0,
        "max_iterations": 0,
        "history": [],
        "file_path": None,
        "commit_sha": None,
        "pr_url": None,
        "ci_result": {},
        "final_result": {},
        "errors": [],
        "status": "running",
        "current_stage": "parsing",
        "progress": 0,
        "should_run_ci": False,
        "auto_commit": False,
        "create_pr": False,
        "run_sandbox_dry_run": False,
        "repair_made": True,
        "save_ok": None,
        "pushed": False,
        "git_branch": None,
    }
    result = asyncio.run(graph.ainvoke(state))
    assert result["status"] == "completed"
    assert result["final_result"]["summary"]["final_status"] == "Validated only"
    assert result["test_code"]
    assert {event["stage"] for event in result["history"]} >= {
        "parsing",
        "generating",
        "validating",
        "saving",
        "ci_running",
        "collecting",
    }
