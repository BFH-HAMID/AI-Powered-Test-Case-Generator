"""Repair node that feeds validation failures back to the LLM and records a code diff."""

from __future__ import annotations

import time
from typing import Any, Dict, List

from langchain_core.messages import HumanMessage, SystemMessage

from app.agent.llm import get_chat_model
from app.agent.nodes._common import compute_unified_diff, record_stage
from app.agent.nodes.generate_tests import extract_code_block
from app.agent.prompts import FIX_TESTS_SYSTEM_PROMPT, fix_prompt
from app.core.logging import get_logger

logger = get_logger(__name__)


def fix_tests_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """LangGraph node: request the smallest possible correction and preserve iteration history."""
    started = time.perf_counter()
    old_code = str(state.get("test_code", ""))
    report = state.get("validation_report") or {}
    options = state.get("options") or {}
    language = str(state.get("language") or "python")
    framework = str(state.get("framework") or "pytest")
    module_name = str(state.get("module_name") or "target_module")

    error_parts = list(report.get("errors") or [])
    error_parts.extend(report.get("security_issues") or [])
    error_parts.extend(report.get("lint_issues") or [])
    dry = report.get("dry_run_results") or {}
    if dry.get("stderr"):
        error_parts.append(f"Dry-run stderr:\n{dry['stderr']}")
    for case in dry.get("test_cases") or []:
        if case.get("status") in {"failed", "error"}:
            error_parts.append(f"{case.get('name')}: {case.get('message') or case.get('status')}")
    if report.get("collect_output"):
        error_parts.append(f"Collection output:\n{report['collect_output']}")

    passing = [
        str(case.get("name"))
        for case in dry.get("test_cases") or []
        if case.get("status") == "passed"
    ]
    errors_text = (
        "\n".join(f"- {error}" for error in error_parts)
        or "Validation failed without a detailed error."
    )
    chat_model = get_chat_model(options.get("model"))
    new_code = old_code
    node_errors: List[str] = []

    if chat_model is None:
        node_errors.append(
            "No configured LLM is available for automatic test repair; original code was retained."
        )
    else:
        try:
            response = chat_model.invoke(
                [
                    SystemMessage(content=FIX_TESTS_SYSTEM_PROMPT),
                    HumanMessage(
                        content=fix_prompt(
                            test_code=old_code,
                            errors=errors_text,
                            passing_tests="\n".join(passing),
                            language=language,
                            framework=framework,
                            module_name=module_name,
                        )
                    ),
                ]
            )
            candidate = extract_code_block(response)
            if candidate.strip():
                new_code = candidate
            else:
                raise ValueError("Repair model returned empty test code")
        except Exception as exc:
            node_errors.append(f"Automatic repair failed: {exc}")
            logger.exception(
                "LLM repair failed", extra={"run_id": state.get("run_id"), "stage": "fixing"}
            )

    result = {
        **state,
        "test_code": new_code,
        "repair_made": new_code != old_code,
        "iteration": int(state.get("iteration", 0)) + 1,
        "errors": list(state.get("errors") or []) + node_errors,
    }
    diff = compute_unified_diff(
        old_code, new_code, fromfile="generated_tests", tofile="repaired_tests"
    )
    record_stage(
        result,
        "fixing",
        started,
        input_summary=f"Fix attempt {result['iteration']}; {len(error_parts)} validation issue(s)",
        output_summary=(
            f"Updated test code ({len(new_code.splitlines())} lines)"
            if new_code != old_code
            else "No repair was made; the original test code was retained"
        ),
        status="ok" if new_code != old_code else "no_change",
        errors=error_parts + node_errors,
        code_snapshot=new_code,
        diff=diff,
        current_stage="fixing",
        progress=min(70, 50 + 5 * int(result["iteration"])),
        run_status="running",
        updates={"test_code": new_code, "iteration": result["iteration"]},
    )
    return result
