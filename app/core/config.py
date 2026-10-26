"""Application configuration loaded from environment variables and .env file."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Global application settings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # General
    app_name: str = "AI-Powered Test Case Generator"
    app_env: str = "development"
    log_level: str = "INFO"
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    backend_url: str = "http://127.0.0.1:8000"

    # Security & Auth
    api_key: str = ""
    github_webhook_secret: str = "dev-webhook-secret-change-me"

    # Database
    database_url: str = "sqlite:///./ai_test_gen.db"

    # LLM Settings
    llm_provider: str = "local-synthesizer"
    llm_model: str = "gpt-4o"
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    ollama_base_url: str = "http://localhost:11434"
    llm_timeout_seconds: int = 45

    # Sandbox & Validation Settings
    sandbox_mode: str = (
        "docker"  # "docker" is isolated; "subprocess" is an explicit trusted-local development fallback
    )
    sandbox_docker_image: str = "ai-test-sandbox:latest"
    sandbox_docker_image_js: str = "ai-test-sandbox-js:latest"
    sandbox_timeout_seconds: int = 30
    sandbox_memory_limit_mb: int = 256
    sandbox_cpu_limit: float = 1.0
    max_iterations_default: int = 3
    node_timeout_seconds: int = 180

    # File System & Git Settings
    project_root: Path = Path(__file__).resolve().parents[2]
    allowed_test_dir: str = "tests/generated"
    git_repo_path: Optional[str] = None
    git_isolated_workspace: str = ""
    github_token: str = ""
    github_default_repo: str = ""
    github_default_branch: str = "main"
    artifact_bucket: str = ""
    artifact_prefix: str = "generated-tests"

    # Optional AWS ECS Fargate dry-run worker (required for isolated Lambda test execution).
    ecs_cluster_arn: str = ""
    ecs_task_definition_arn: str = ""
    ecs_subnets: str = ""
    ecs_security_group_id: str = ""
    ecs_container_name: str = "test-sandbox"
    ecs_task_timeout_seconds: int = 300


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached settings instance."""
    return Settings()
