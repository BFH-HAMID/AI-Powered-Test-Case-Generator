"""Tests for deterministic local test generation and safe expected-value inference."""

from __future__ import annotations

from app.agent.nodes.generate_tests import local_generate_tests
from app.agent.nodes.parse_requirement import _local_parse_requirement
from app.tools.code_analyzer import analyze_python_code


def test_local_synthesizer_asserts_simple_pure_return_behavior() -> None:
    source = """
def add(a: int, b: int) -> int:
    return a + b
"""
    analysis = analyze_python_code(source)
    spec = _local_parse_requirement("add(a, b) returns the sum.", analysis)
    code = local_generate_tests(
        {
            "source_code": source,
            "module_name": "calculator",
            "language": "python",
            "framework": "pytest",
            "parsed_spec": spec.model_dump(mode="json"),
            "code_analysis": analysis.to_dict(),
        }
    )
    assert "actual = target.add(1, 1)" in code
    assert "assert actual == 2" in code


def test_local_synthesizer_uses_unittest_assertion_for_simple_behavior() -> None:
    source = """
def multiply(left: int, right: int) -> int:
    return left * right
"""
    analysis = analyze_python_code(source)
    spec = _local_parse_requirement("multiply(left, right) returns a product.", analysis)
    code = local_generate_tests(
        {
            "source_code": source,
            "module_name": "calculator",
            "language": "python",
            "framework": "unittest",
            "parsed_spec": spec.model_dump(mode="json"),
            "code_analysis": analysis.to_dict(),
        }
    )
    assert "actual = target.multiply(1, 1)" in code
    assert "self.assertEqual(actual, 1)" in code
