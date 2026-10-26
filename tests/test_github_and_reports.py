"""Tests for GitHub signature verification, JUnit parsing, and report generation."""

from __future__ import annotations

import hashlib
import hmac
import zipfile
from io import BytesIO

from app.tools.github_tool import normalize_repo_slug, verify_github_webhook_signature
from app.tools.report_exporter import (
    generate_markdown_report,
    generate_pdf_report,
    generate_zip_bundle,
)
from app.tools.sandbox_runner import parse_junit_xml


def test_github_webhook_signature_and_repo_normalization() -> None:
    body = b'{"ref":"refs/heads/main"}'
    secret = "test-webhook-secret"
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert verify_github_webhook_signature(body, f"sha256={digest}", secret)
    assert not verify_github_webhook_signature(body, "sha256=bad", secret)
    assert not verify_github_webhook_signature(body, f"sha256={digest}", "")
    assert normalize_repo_slug("https://github.com/acme/example.git") == "acme/example"
    assert normalize_repo_slug("acme/example") == "acme/example"
    assert normalize_repo_slug("https://evil.example/acme/example") is None


def test_junit_xml_parsing() -> None:
    xml = """<testsuites><testsuite tests="3" failures="1" skipped="1">
      <testcase classname="tests.test_math" name="test_add" time="0.01" />
      <testcase classname="tests.test_math" name="test_bad" time="0.02"><failure message="expected 3">trace</failure></testcase>
      <testcase classname="tests.test_math" name="test_skip" time="0"><skipped message="not configured" /></testcase>
    </testsuite></testsuites>"""
    total, passed, failed, skipped, duration, cases = parse_junit_xml(xml)
    assert (total, passed, failed, skipped) == (3, 1, 1, 1)
    assert duration == 30.0
    assert cases[1].marker == "❌"
    assert cases[2].status == "skipped"


def test_markdown_pdf_and_zip_report_export() -> None:
    data = {
        "run_id": "r-123",
        "requirement": "add(a, b) returns the sum",
        "language": "python",
        "framework": "pytest",
        "module_name": "calculator",
        "test_code": "def test_add():\n    assert 1 + 2 == 3\n",
        "final_result": {
            "summary": {
                "total": 1,
                "passed": 1,
                "failed": 0,
                "coverage_pct": 100,
                "auto_fixes": 0,
                "duration_ms": 1.0,
                "final_status": "Passed",
                "badge": "✅ Passed",
            },
            "test_results": [
                {"name": "test_add", "status": "passed", "marker": "✅", "duration_ms": 1.0}
            ],
        },
    }
    markdown = generate_markdown_report(data)
    assert "Requirement Specification" in markdown
    assert "test_add" in markdown
    pdf = generate_pdf_report(data)
    assert pdf.startswith(b"%PDF")
    bundle = generate_zip_bundle(data)
    with zipfile.ZipFile(BytesIO(bundle)) as zf:
        assert "REPORT.md" in zf.namelist()
        assert "REPORT.pdf" in zf.namelist()
