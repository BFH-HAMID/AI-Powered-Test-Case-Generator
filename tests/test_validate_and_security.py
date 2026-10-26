"""Unit tests for security guard, linter, syntax check, and sandbox runner."""

from __future__ import annotations

from app.tools.linter import check_security, check_syntax, format_and_lint
from app.tools.sandbox_runner import run_collection_check, run_sandbox_dry_run


def test_security_guard_rejects_dangerous_code() -> None:
    bad_samples = [
        "import os\ndef test_hack():\n    os.system('rm -rf /')\n",
        "import subprocess\ndef test_sub():\n    subprocess.run(['ls'])\n",
        "def test_eval():\n    assert eval('1 + 1') == 2\n",
        "def test_exec():\n    exec('x = 1')\n",
        "import socket\ndef test_sock():\n    s = socket.socket()\n",
        "def test_etc():\n    with open('/etc/passwd') as f:\n        pass\n",
    ]
    for code in bad_samples:
        res = check_security(code, "python")
        assert res.ok is False, f"Expected security failure for:\n{code}"
        assert len(res.issues) > 0


def test_security_guard_allows_safe_tests() -> None:
    safe_code = """
import pytest
from unittest.mock import patch

def test_safe_tmp(tmp_path):
    p = tmp_path / "sample.txt"
    p.write_text("hello")
    assert p.read_text() == "hello"
"""
    res = check_security(safe_code, "python")
    assert res.ok is True
    assert res.issues == []


def test_syntax_and_linter() -> None:
    broken = "def test_broken(\n    assert True"
    syn = check_syntax(broken, "python")
    assert syn.ok is False
    assert syn.error is not None

    messy = "def test_add( ):\n    x=1+2\n    assert x==3\n"
    syn_ok = check_syntax(messy, "python")
    assert syn_ok.ok is True
    lint_res = format_and_lint(messy, "python")
    assert lint_res.ok is True
    assert "def test_add():" in lint_res.formatted_code


def test_sandbox_collection_and_dry_run_with_coverage(monkeypatch) -> None:
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "sandbox_mode", "subprocess")
    source = """
def add(a: int, b: int) -> int:
    if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
        raise TypeError("Arguments must be numeric")
    return a + b
"""
    tests = """
import pytest
from math_utils import add

def test_add_happy_path():
    assert add(2, 3) == 5

def test_add_negative_numbers():
    assert add(-4, 1) == -3

def test_add_invalid_type_raises():
    with pytest.raises(TypeError):
        add("bad", 2)
"""
    col = run_collection_check(tests, source_code=source, module_name="math_utils")
    assert col.ok is True
    assert col.collected_count >= 3

    dry = run_sandbox_dry_run(tests, source_code=source, module_name="math_utils")
    assert dry.executed is True
    assert dry.exit_code == 0
    assert dry.total == 3
    assert dry.passed == 3
    assert dry.failed == 0
    assert dry.coverage_pct >= 90.0
    assert all(tc.marker == "✅" for tc in dry.test_cases)


def test_default_sandbox_never_runs_generated_code_on_host(monkeypatch) -> None:
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "sandbox_mode", "docker")
    result = run_sandbox_dry_run("def test_safe():\\n    assert 1 == 1\\n")
    assert result.sandbox_backend != "explicit-local-subprocess"
    if result.sandbox_backend == "docker-unavailable":
        assert result.executed is False


def test_sandbox_blocks_outbound_network(monkeypatch) -> None:
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "sandbox_mode", "subprocess")
    net_test = """
import urllib.request

def test_outbound_blocked():
    urllib.request.urlopen("http://93.184.216.34", timeout=1)
"""
    dry = run_sandbox_dry_run(net_test, source_code="", module_name="dummy_mod")
    assert dry.executed is True
    assert dry.failed == 1
    assert dry.exit_code != 0
