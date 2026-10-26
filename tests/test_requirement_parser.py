"""Five representative requirement parsing examples for the offline structured parser."""

from __future__ import annotations

import pytest

from app.agent.nodes.parse_requirement import _local_parse_requirement
from app.tools.code_analyzer import CodeAnalysisResult, analyze_python_code


@pytest.mark.parametrize(
    "requirement,expected_function,expect_negative,expect_edge",
    [
        (
            "calculate_total(items) returns the sum of item prices and handles an empty list.",
            "calculate_total",
            False,
            True,
        ),
        (
            "validate_email(address) rejects invalid addresses with ValueError.",
            "validate_email",
            True,
            True,
        ),
        (
            "paginate(items, page, size) returns the requested page and rejects negative page numbers.",
            "paginate",
            True,
            True,
        ),
        (
            "normalize_name(value) trims whitespace and preserves unicode characters.",
            "normalize_name",
            False,
            True,
        ),
        (
            "divide(a, b) returns the quotient; division by zero raises ZeroDivisionError.",
            "divide",
            True,
            True,
        ),
    ],
)
def test_parse_five_sample_requirements(
    requirement: str,
    expected_function: str,
    expect_negative: bool,
    expect_edge: bool,
) -> None:
    spec = _local_parse_requirement(requirement, CodeAnalysisResult(language="python"))
    names = [fn.name for fn in spec.functions_under_test]
    assert expected_function in names
    if expect_negative:
        assert spec.negative_tests
    if expect_edge:
        assert spec.edge_cases
    assert spec.core_functionality


def test_source_analysis_is_ground_truth_for_functions() -> None:
    analysis = analyze_python_code("""
def actual_name(value: str) -> str:
    \"\"\"Return the normalized name.\"\"\"
    return value.strip()
""")
    spec = _local_parse_requirement(
        "Do not invent phantom() functions; actual_name(value) trims whitespace.", analysis
    )
    assert [fn.name for fn in spec.functions_under_test] == ["actual_name"]
    assert "phantom" not in [fn.name for fn in spec.functions_under_test]
