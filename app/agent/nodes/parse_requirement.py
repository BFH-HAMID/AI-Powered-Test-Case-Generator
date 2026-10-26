"""Requirement parsing node: grounds structured test specifications in source analysis facts."""

from __future__ import annotations

import re
import time
from typing import Any, Dict, List

from langchain_core.messages import HumanMessage, SystemMessage

from app.agent.llm import get_chat_model
from app.agent.nodes._common import json_safe, record_stage
from app.agent.prompts import REQUIREMENT_PARSE_SYSTEM_PROMPT, parse_prompt
from app.api.schemas import ParsedRequirementSpec
from app.core.logging import get_logger
from app.tools.code_analyzer import CodeAnalysisResult, analyze_source_code

logger = get_logger(__name__)


_FUNCTION_CALL_RE = re.compile(r"(?<![A-Za-z0-9_])([A-Za-z_][A-Za-z0-9_]*)\s*\(([^()]*)\)")


def _purpose_for_function(fn: Dict[str, Any]) -> str:
    """Choose a grounded function purpose description."""
    doc = (fn.get("docstring") or "").strip()
    return (
        doc.splitlines()[0]
        if doc
        else f"Exercise behavior of {fn.get('qualname') or fn.get('name')}"
    )


def _source_functions(analysis: CodeAnalysisResult) -> List[Dict[str, str]]:
    """Flatten module functions and public class methods from source analysis."""
    funcs: List[Dict[str, str]] = []
    for fn in analysis.functions:
        funcs.append(
            {
                "name": fn.name,
                "signature": fn.signature,
                "purpose": _purpose_for_function(fn.__dict__),
            }
        )
    for cls in analysis.classes:
        for method in cls.methods:
            if method.name == "__init__":
                continue
            funcs.append(
                {
                    "name": f"{cls.name}.{method.name}",
                    "signature": method.signature,
                    "purpose": _purpose_for_function(method.__dict__),
                }
            )
    return funcs


def _local_parse_requirement(
    requirement: str,
    analysis: CodeAnalysisResult,
) -> ParsedRequirementSpec:
    """Deterministic offline parser used when no LLM credentials are configured."""
    source_functions = _source_functions(analysis)
    # Do not infer implementation symbols when source exists; source analysis is authoritative.
    if source_functions:
        funcs = source_functions
    else:
        funcs = []
        for name, raw_args in _FUNCTION_CALL_RE.findall(requirement):
            if name.lower() in {"if", "for", "while", "when", "given", "then", "test", "assert"}:
                continue
            args = [p.strip() for p in raw_args.split(",") if p.strip()]
            signature = f"{name}({', '.join(args)})"
            if not any(f["name"] == name for f in funcs):
                funcs.append(
                    {
                        "name": name,
                        "signature": signature,
                        "purpose": f"Behavior described for {name}",
                    }
                )

    sentences = [
        s.strip(" \t\r\n-*•")
        for s in re.split(r"(?<=[.!?])\s+|\n+", requirement)
        if s.strip(" \t\r\n-*•")
    ]
    core_functionality = sentences[:12]

    edge_words = re.compile(
        r"\b(edge|boundary|empty|null|none|zero|negative|positive|maximum|minimum|limit|overflow|underflow|whitespace|unicode|duplicate|missing|invalid)\b",
        re.IGNORECASE,
    )
    edge_cases = [s for s in sentences if edge_words.search(s)][:12]

    negative_tests = []
    for sentence in sentences:
        if re.search(
            r"\b(invalid|reject(?:s|ed)?|rais(?:e|es)|throw(?:s)?|error|exception|must not|cannot|not allow|division by zero)\b",
            sentence,
            re.I,
        ):
            expected = "Expected error behavior is not specified; verify the documented exception or rejection behavior."
            exception_match = re.search(r"\b([A-Z][A-Za-z0-9]*(?:Error|Exception))\b", sentence)
            if exception_match:
                expected = exception_match.group(1)
            elif re.search(r"\b(400|401|403|404)\b", sentence):
                expected = re.search(r"\b(400|401|403|404)\b", sentence).group(1)
            elif re.search(r"division by zero", sentence, re.I):
                expected = "ZeroDivisionError"
            negative_tests.append({"scenario": sentence[:500], "expected_error": expected})

    dependencies = sorted(set(analysis.external_dependencies))
    assumptions: List[str] = []
    if not analysis.functions and not analysis.classes:
        assumptions.append(
            "No source functions or classes were available. The generated tests use any explicit function names in the requirement; provide source code to validate behavior against an implementation."
        )
    if not analysis.syntax_valid and analysis.syntax_error:
        assumptions.append(f"Source code has a syntax issue: {analysis.syntax_error}")

    if not sentences:
        core_functionality = [requirement.strip()]

    # Collect explicit expected outputs with a conservative simple pattern.
    expected_behavior = []
    for sentence in sentences:
        match = re.search(
            r"(?:input|when given|for)\s+(.+?)\s+(?:should|must|returns?|produce|yield)\s+(.+)$",
            sentence,
            re.I,
        )
        if match:
            expected_behavior.append(
                {"input": match.group(1).strip(), "expected_output": match.group(2).strip()}
            )

    return ParsedRequirementSpec(
        functions_under_test=funcs,
        core_functionality=core_functionality,
        expected_behavior=expected_behavior,
        edge_cases=edge_cases,
        negative_tests=negative_tests,
        dependencies_to_mock=dependencies,
        setup_teardown_needs=[],
        assumptions=assumptions,
    )


def parse_requirement_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """LangGraph node: analyze code and parse requirement into a validated Pydantic spec."""
    started = time.perf_counter()
    requirement = str(state.get("requirement", ""))
    source_code = state.get("source_code")
    language = str(state.get("language", "python"))

    analysis = (
        analyze_source_code(source_code, language)
        if source_code
        else CodeAnalysisResult(language=language)
    )
    analysis_dict = analysis.to_dict()
    parsed: ParsedRequirementSpec
    errors: List[str] = []

    chat_model = get_chat_model((state.get("options") or {}).get("model"))
    if chat_model is not None:
        try:
            structured = chat_model.with_structured_output(ParsedRequirementSpec)
            response = structured.invoke(
                [
                    SystemMessage(content=REQUIREMENT_PARSE_SYSTEM_PROMPT),
                    HumanMessage(
                        content=parse_prompt(
                            requirement=requirement,
                            source_analysis_json=json_safe(analysis_dict),
                            language=language,
                        )
                    ),
                ]
            )
            parsed = (
                response
                if isinstance(response, ParsedRequirementSpec)
                else ParsedRequirementSpec.model_validate(response)
            )
            if source_code and analysis.functions + [
                m for c in analysis.classes for m in c.methods
            ]:
                allowed = {fn["name"] for fn in _source_functions(analysis)}
                # Ground implementation targets to names found by the analyzer.
                parsed.functions_under_test = [
                    fn
                    for fn in parsed.functions_under_test
                    if fn.name in allowed or fn.name.split(".")[-1] in allowed
                ]
                if not parsed.functions_under_test:
                    parsed.functions_under_test = [
                        # Pydantic will enforce the declared schema.
                        *ParsedRequirementSpec.model_validate(
                            {"functions_under_test": _source_functions(analysis)}
                        ).functions_under_test
                    ]
        except Exception as exc:
            logger.warning(
                "Structured requirement parsing failed; using deterministic parser",
                extra={"run_id": state.get("run_id"), "stage": "parsing"},
            )
            errors.append(f"LLM requirement parsing unavailable: {exc}")
            parsed = _local_parse_requirement(requirement, analysis)
    else:
        parsed = _local_parse_requirement(requirement, analysis)

    generation_options = state.get("options") or {}
    if not bool(generation_options.get("include_edge_cases", True)):
        parsed.edge_cases = []
    if not bool(generation_options.get("include_negative_tests", True)):
        parsed.negative_tests = []

    result = {
        **state,
        "parsed_spec": parsed.model_dump(mode="json"),
        "code_analysis": analysis_dict,
        "errors": list(state.get("errors") or []) + errors,
    }
    record_stage(
        result,
        "parsing",
        started,
        input_summary=f"Requirement ({len(requirement)} chars), source ({len(source_code or '')} chars), language={language}",
        output_summary=f"Found {len(parsed.functions_under_test)} target function(s), {len(parsed.edge_cases)} edge case(s), {len(parsed.negative_tests)} negative test(s)",
        status="ok",
        errors=errors,
        current_stage="parsing",
        progress=10,
        run_status="running",
        updates={"parsed_spec": parsed.model_dump(mode="json")},
    )
    return result
