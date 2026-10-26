"""JavaScript syntax, test-discovery, and sandbox harness tests."""

from __future__ import annotations

from app.core.config import get_settings
from app.tools.linter import check_security, check_syntax
from app.tools.sandbox_runner import run_collection_check, run_sandbox_dry_run


def test_javascript_generation_checks_and_harness(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "sandbox_mode", "subprocess")
    source = "function add(a, b) { return a + b; }"
    tests = """
const target = require("./math");
describe("add", () => {
  test("returns the sum", () => {
    expect(target.add(2, 3)).toBe(5);
  });
});
"""
    assert check_syntax(tests, "javascript").ok
    assert check_security(tests, "javascript").ok
    collected = run_collection_check(tests, source, "math", "javascript", "jest")
    assert collected.ok and collected.collected_count == 1
    result = run_sandbox_dry_run(tests, source, "math", "javascript", "jest")
    assert result.executed
    assert result.exit_code == 0
    assert result.passed == 1


def test_javascript_network_execution_is_rejected() -> None:
    result = check_security("test('network', () => fetch('https://example.com'));", "javascript")
    assert not result.ok
    assert any("network" in issue.lower() for issue in result.issues)
