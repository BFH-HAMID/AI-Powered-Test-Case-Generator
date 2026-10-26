"""GitHub webhook route security and payload tests."""

from __future__ import annotations

import hashlib
import hmac
import json

from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.db.session import reset_db_engine
from app.main import app
from app.tools.github_tool import verify_github_webhook_signature


def test_github_webhook_requires_hmac_and_accepts_empty_push(tmp_path, monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'webhook.db'}")
    monkeypatch.setattr(settings, "github_webhook_secret", "hook-secret")
    monkeypatch.setattr(settings, "api_key", "")
    reset_db_engine()
    body = json.dumps(
        {
            "ref": "refs/heads/main",
            "after": "deadbeef",
            "repository": {"full_name": "example/project"},
            "commits": [],
        }
    ).encode()
    signature = "sha256=" + hmac.new(b"hook-secret", body, hashlib.sha256).hexdigest()

    with TestClient(app) as client:
        denied = client.post(
            "/webhook/github",
            content=body,
            headers={"X-GitHub-Event": "push", "X-Hub-Signature-256": "sha256=bad"},
        )
        assert denied.status_code == 401

        accepted = client.post(
            "/webhook/github",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-GitHub-Event": "push",
                "X-Hub-Signature-256": signature,
            },
        )
        assert accepted.status_code == 202
        assert accepted.json()["created_runs"] == []

    monkeypatch.setattr(settings, "github_webhook_secret", "")
    assert not verify_github_webhook_signature(body, signature)
