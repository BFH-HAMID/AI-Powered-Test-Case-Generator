"""Unit tests for app/tools/code_analyzer.py."""

from __future__ import annotations

from app.tools.code_analyzer import analyze_source_code


def test_analyze_python_functions_and_classes() -> None:
    source = '''
import requests

MAX_RETRIES = 3

def calculate_discount(price: float, rate: float = 0.1) -> float:
    """Compute discounted price."""
    if price < 0:
        raise ValueError("Price cannot be negative")
    if rate < 0 or rate > 1:
        raise ValueError("Invalid discount rate")
    return round(price * (1 - rate), 2)

class PaymentGateway:
    """Handles remote payments."""
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    async def charge(self, amount: float) -> dict:
        if amount <= 0:
            raise RuntimeError("Amount must be positive")
        resp = requests.post("https://example.com/pay")
        return {"ok": True}
'''
    analysis = analyze_source_code(source, "python")
    assert analysis.syntax_valid is True
    assert len(analysis.functions) == 1
    fn = analysis.functions[0]
    assert fn.name == "calculate_discount"
    assert fn.return_annotation == "float"
    assert fn.docstring == "Compute discounted price."
    assert "ValueError" in fn.raised_exceptions
    assert any("price < 0" in b for b in fn.branches)
    assert len(analysis.classes) == 1
    cls = analysis.classes[0]
    assert cls.name == "PaymentGateway"
    assert len(cls.methods) == 2
    assert "requests" in analysis.external_dependencies
    assert "MAX_RETRIES" in analysis.constants


def test_analyze_python_syntax_error() -> None:
    broken = "def foo(x:\n    return x"
    analysis = analyze_source_code(broken, "python")
    assert analysis.syntax_valid is False
    assert analysis.syntax_error is not None


def test_analyze_javascript_and_java() -> None:
    js_code = """
export function divide(a, b = 1) {
    if (b === 0) {
        throw new RangeError("Division by zero");
    }
    return a / b;
}
"""
    js_analysis = analyze_source_code(js_code, "javascript")
    assert len(js_analysis.functions) == 1
    assert js_analysis.functions[0].name == "divide"
    assert "RangeError" in js_analysis.functions[0].raised_exceptions

    java_code = """
public class StringUtils {
    public static String reverse(String input) {
        if (input == null) {
            throw new IllegalArgumentException("Input must not be null");
        }
        return new StringBuilder(input).reverse().toString();
    }
}
"""
    java_analysis = analyze_source_code(java_code, "java")
    assert len(java_analysis.classes) == 1
    assert java_analysis.classes[0].name == "StringUtils"
    assert len(java_analysis.functions) == 1
    assert java_analysis.functions[0].name == "reverse"
    assert "IllegalArgumentException" in java_analysis.functions[0].raised_exceptions
