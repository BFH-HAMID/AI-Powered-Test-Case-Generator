"""Prompt templates and language/framework few-shot examples for test generation."""

from __future__ import annotations

from typing import Dict

REQUIREMENT_PARSE_SYSTEM_PROMPT = """You are a senior software quality engineer. Convert a user requirement into a precise, conservative test specification.

Rules:
- Return the requested structured schema, not prose.
- Never invent source functions, classes, signatures, dependencies, or behavior.
- If source analysis facts are supplied, only identify functions in those facts as implementation targets. Requirement-described expected behavior may be captured separately.
- Separate explicit requirements from assumptions. Put uncertainty in assumptions.
- Include positive behavior, boundaries, edge cases, and negative scenarios only when supported by the requirement or source facts.
- Identify network, database, filesystem, clock, randomness, and other external dependencies that should be mocked.
- Keep each entry concise, concrete, and testable.
"""

TEST_GENERATION_SYSTEM_PROMPT = """You are an expert test engineer writing safe, deterministic unit tests.

Produce tests for the specified language and framework. Follow these rules:
1. Cover only the supplied parsed specification and actual source-analysis facts. Do not invent APIs.
2. Use the exact import module path supplied by the user; Python tests should import that module, not reimplement it.
3. Include happy paths, boundaries, edge cases, and negative cases when the specification supports them. Use fixtures/parameterization where appropriate and setup/teardown only when needed.
4. Follow Arrange–Act–Assert and descriptive test_<function>_<scenario> names (or framework-equivalent names).
5. Mock all network, database, external API, file I/O, time, and random dependencies. Never make a real network call, write outside a framework-provided temporary directory, or depend on nondeterministic state.
6. Do not use os.system, subprocess, eval, exec, sockets, shell commands, or dynamic imports.
7. Python: use pytest unless unittest is requested. JavaScript: use Jest. Java: use JUnit 5.
8. The response must contain only one fenced code block with the tests and no prose. Do not include Markdown explanations.
"""

FIX_TESTS_SYSTEM_PROMPT = """You are repairing a generated test suite. Change only what the validation report proves is broken. Preserve tests that already pass and keep the same language, framework, module import, and intent. Do not weaken assertions to hide product failures. Keep all tests deterministic and safe: no network, subprocess, shell commands, eval/exec, sockets, or writes outside temporary directories. Return only one fenced code block containing the complete corrected test file; no prose.
"""

FEW_SHOT_EXAMPLES: Dict[str, str] = {
    "python_pytest": """Requirement: add(a, b) returns the numeric sum; invalid non-numeric inputs raise TypeError.\n\n```python\nimport pytest\nfrom calculator import add\n\n\ndef test_add_returns_sum_for_positive_values():\n    # Arrange\n    left, right = 2, 3\n\n    # Act\n    actual = add(left, right)\n\n    # Assert\n    assert actual == 5\n\n\ndef test_add_rejects_non_numeric_input():\n    # Arrange / Act / Assert\n    with pytest.raises(TypeError):\n        add("two", 3)\n```""",
    "python_unittest": """Requirement: is_even(n) returns True for even integers and False for odd integers.\n\n```python\nimport unittest\nfrom number_utils import is_even\n\n\nclass TestIsEven(unittest.TestCase):\n    def test_is_even_returns_true_for_even_number(self):\n        # Arrange\n        value = 8\n\n        # Act\n        actual = is_even(value)\n\n        # Assert\n        self.assertTrue(actual)\n\n    def test_is_even_returns_false_for_odd_number(self):\n        self.assertFalse(is_even(7))\n```""",
    "javascript_jest": """Requirement: add(a, b) returns the numeric sum.\n\n```javascript\nconst { add } = require("./calculator");\n\ndescribe("add", () => {\n  test("returns the sum of two numbers", () => {\n    // Arrange\n    const left = 2;\n    const right = 3;\n\n    // Act\n    const actual = add(left, right);\n\n    // Assert\n    expect(actual).toBe(5);\n  });\n});\n```""",
    "java_junit": """Requirement: StringUtils.reverse(input) returns the characters in reverse order.\n\n```java\nimport static org.junit.jupiter.api.Assertions.assertEquals;\nimport org.junit.jupiter.api.Test;\n\nclass StringUtilsTest {\n    @Test\n    void reverseReturnsCharactersInReverseOrder() {\n        // Arrange\n        String input = "hello";\n\n        // Act\n        String actual = StringUtils.reverse(input);\n\n        // Assert\n        assertEquals("olleh", actual);\n    }\n}\n```""",
}


def parse_prompt(requirement: str, source_analysis_json: str, language: str) -> str:
    """Build a grounded parse prompt with implementation facts."""
    return f"""Parse the following requirement into the supplied Pydantic schema.\n\nProgramming language: {language}\n\nUSER REQUIREMENT:\n{requirement}\n\nSOURCE ANALYSIS FACTS (authoritative when present; never invent functions):\n{source_analysis_json}\n\nReturn every schema field, using empty lists when no supported items exist. Flag ambiguity as assumptions."""


def generation_prompt(
    requirement: str,
    parsed_spec_json: str,
    source_analysis_json: str,
    language: str,
    framework: str,
    module_name: str,
) -> str:
    """Build language-specific generation prompt with optional few-shot example."""
    key = f"{language.lower()}_{framework.lower()}"
    example = FEW_SHOT_EXAMPLES.get(key, "")
    return f"""Create the complete test file now.\n\nLanguage: {language}\nFramework: {framework}\nExact source import module path: {module_name}\n\nRequirement:\n{requirement}\n\nParsed specification (JSON):\n{parsed_spec_json}\n\nSource analysis facts (JSON):\n{source_analysis_json}\n\n{('Style example, adapt only the format and not its behavior:' + chr(10) + example + chr(10)) if example else ''}\nReturn one code block only."""


def fix_prompt(
    test_code: str,
    errors: str,
    passing_tests: str,
    language: str,
    framework: str,
    module_name: str,
) -> str:
    """Build a code repair prompt with validation output and passing test names."""
    return f"""Language: {language}\nFramework: {framework}\nImport module path: {module_name}\n\nPassing tests that must remain unchanged in intent:\n{passing_tests or 'No passing tests were identified.'}\n\nComplete generated test file:\n```\n{test_code}\n```\n\nValidation errors to fix:\n{errors}\n\nReturn the full corrected file in one fenced code block only."""
