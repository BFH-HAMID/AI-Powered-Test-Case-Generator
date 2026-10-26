"""FastAPI endpoint and background generation integration tests."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.db.session import create_run_record, reset_db_engine, update_run_stage
from app.main import app


def test_health_and_background_generate_flow(tmp_path, monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'api_test.db'}")
    monkeypatch.setattr(settings, "project_root", tmp_path)
    monkeypatch.setattr(settings, "sandbox_mode", "docker")
    monkeypatch.setattr(settings, "api_key", "")
    monkeypatch.setattr(settings, "llm_provider", "local-synthesizer")
    reset_db_engine()

    with TestClient(app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"
        assert health.headers.get("x-request-id")

        response = client.post(
            "/generate",
            json={
                "requirement": "add(a, b) returns the sum of the two numbers.",
                "source_code": "def add(a: int, b: int) -> int:\n    return a + b\n",
                "module_name": "calc",
                "language": "python",
                "framework": "pytest",
                "options": {
                    "model": "local-synthesizer",
                    "max_retries": 0,
                    "run_sandbox_dry_run": False,
                    "auto_commit": False,
                    "trigger_ci": False,
                },
            },
        )
        assert response.status_code == 202
        run_id = response.json()["run_id"]

        status_response = client.get(f"/status/{run_id}")
        assert status_response.status_code == 200
        assert status_response.json()["progress"] == 100
        assert status_response.json()["status"] == "completed"
        assert status_response.json()["partial_outputs"]["test_code"]

        result = client.get(f"/result/{run_id}")
        assert result.status_code == 200
        payload = result.json()
        assert payload["run_id"] == run_id
        assert "test_add" in payload["test_code"]
        assert payload["summary"]["final_status"] in {"Validated only", "Not Run", "Passed"}
        assert payload["validation_report"]["syntax_ok"] is True
        assert payload["history"]

        history = client.get("/history")
        assert history.status_code == 200
        assert any(item["run_id"] == run_id for item in history.json()["items"])

        feedback = client.post(
            f"/feedback/{run_id}",
            json={"rating": 5, "comment": "Clear smoke test", "helpful_tags": ["clear"]},
        )
        assert feedback.status_code == 200
        assert feedback.json()["rating"] == 5

        md = client.get(f"/download/{run_id}/report.md")
        assert md.status_code == 200
        assert "Requirement Specification" in md.text


def test_java_commit_uses_test_source_package_path(tmp_path, monkeypatch) -> None:
    settings = get_settings()
    repo_path = tmp_path / "java_repo"
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'java_commit.db'}")
    monkeypatch.setattr(settings, "project_root", tmp_path)
    monkeypatch.setattr(settings, "git_repo_path", str(repo_path))
    monkeypatch.setattr(settings, "github_token", "")
    monkeypatch.setattr(settings, "api_key", "")
    reset_db_engine()

    source_code = """package com.example;
public class Calculator {
    public int add(int a, int b) { return a + b; }
}
"""
    test_code = """package com.example;
import static org.junit.jupiter.api.Assertions.assertEquals;
import org.junit.jupiter.api.Test;

class CalculatorTest {
    @Test
    void testAdd() {
        assertEquals(2, 1 + 1);
    }
}
"""
    run_id = "java-commit-run"

    with TestClient(app) as client:
        create_run_record(
            run_id=run_id,
            requirement="Calculator adds two integers.",
            source_code=source_code,
            module_name="Calculator",
            language="java",
            framework="junit",
            repo_url=None,
            branch="main",
            options={},
            max_iterations=0,
        )
        update_run_stage(
            run_id,
            stage="validated",
            progress=100,
            status="completed",
            test_code=test_code,
        )

        response = client.post(f"/commit/{run_id}", json={})

    assert response.status_code == 200
    result = response.json()
    expected_path = "src/test/java/com/example/CalculatorTest.java"
    assert result["relative_path"] == expected_path
    assert (repo_path / expected_path).read_text(encoding="utf-8") == test_code
    assert result["commit_sha"]


def test_java_github_commit_uses_test_source_package_path(tmp_path, monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'java_remote.db'}")
    monkeypatch.setattr(settings, "github_token", "unit-test-token")
    monkeypatch.setattr(settings, "api_key", "")
    reset_db_engine()

    source_code = """package org.example;
public class Widget {}
"""
    test_code = """package org.example;
class WidgetTest {
    @org.junit.jupiter.api.Test
    void smokeTest() { org.junit.jupiter.api.Assertions.assertTrue(true); }
}
"""
    captured = {}

    def fake_github_commit(**kwargs):
        captured.update(kwargs)
        return {
            "file_path": kwargs["file_path"],
            "branch_name": "ai-tests/java-remote-run",
            "commit_sha": "abc123",
            "pushed": True,
            "pr_url": None,
        }

    monkeypatch.setattr("app.api.routes.commit_file_to_github", fake_github_commit)
    run_id = "java-remote-run"

    with TestClient(app) as client:
        create_run_record(
            run_id=run_id,
            requirement="Widget can be loaded.",
            source_code=source_code,
            module_name="Widget",
            language="java",
            framework="junit",
            repo_url="example/widgets",
            branch="main",
            options={},
            max_iterations=0,
        )
        update_run_stage(
            run_id,
            stage="validated",
            progress=100,
            status="completed",
            test_code=test_code,
        )
        response = client.post(f"/commit/{run_id}", json={})

    assert response.status_code == 200
    assert captured["file_path"] == "src/test/java/org/example/WidgetTest.java"
    assert response.json()["file_path"] == captured["file_path"]


def test_lambda_manual_commit_requires_github_destination(tmp_path, monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'lambda_commit.db'}")
    monkeypatch.setattr(settings, "github_token", "")
    monkeypatch.setattr(settings, "api_key", "")
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "test-generator")
    monkeypatch.setattr(
        "app.api.routes.get_run_with_details",
        lambda _: {
            "run_id": "lambda-run",
            "test_code": """class WidgetTest { void smokeTest() {} }
""",
            "language": "java",
            "module_name": "Widget",
            "source_code": """package org.example;
class Widget {}
""",
            "repo_url": None,
            "branch": "main",
        },
    )

    with TestClient(app) as client:
        response = client.post("/commit/lambda-run", json={})

    assert response.status_code == 400
    assert "GITHUB_TOKEN" in response.json()["detail"]


def test_api_key_protection(tmp_path, monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'auth_test.db'}")
    monkeypatch.setattr(settings, "api_key", "secret-test-key")
    reset_db_engine()

    with TestClient(app) as client:
        denied = client.get("/history")
        assert denied.status_code == 401
        allowed = client.get("/history", headers={"X-API-Key": "secret-test-key"})
        assert allowed.status_code == 200
        assert client.get("/health").status_code == 200

    monkeypatch.setattr(settings, "api_key", "")
