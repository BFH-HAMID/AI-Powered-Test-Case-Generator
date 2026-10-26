"""Validation node: syntax → security → format/lint → collection → sandbox dry-run."""

from __future__ import annotations

import time
from typing import Any, Dict, List

from app.agent.nodes._common import record_stage
from app.api.schemas import DryRunResultSchema, ValidationReportSchema
from app.core.logging import get_logger
from app.tools.linter import check_security, check_syntax, format_and_lint
from app.tools.sandbox_runner import run_collection_check, run_sandbox_dry_run

logger = get_logger(__name__)


def validate_tests_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """LangGraph node: run layered validation and stop on first blocking failure."""
    started = time.perf_counter()
    code = str(state.get("test_code", ""))
    language = str(state.get("language") or "python")
    framework = str(state.get("framework") or "pytest")
    module_name = str(state.get("module_name") or "target_module")
    source_code = state.get("source_code")
    options = state.get("options") or {}
    report = ValidationReportSchema()
    validation_errors: List[str] = []

    # 1. Syntax check is always safe and runs before any test code is imported.
    syntax = check_syntax(code, language)
    report.syntax_ok = syntax.ok
    if not syntax.ok:
        msg = syntax.error or "Generated test code has invalid syntax"
        validation_errors.append(msg)
        report.errors = validation_errors
        report.valid = False
    else:
        # 2. Security scan before any generated test code is collected/executed.
        security = check_security(code, language)
        report.security_ok = security.ok
        report.security_issues = security.issues
        if not security.ok:
            validation_errors.extend(security.issues)
            report.errors = validation_errors
            report.valid = False
        else:
            # 3. Format/lint. Re-scan formatted code before loading it.
            lint = format_and_lint(code, language)
            code = lint.formatted_code or code
            report.formatted_code = code
            report.lint_ok = lint.ok
            report.lint_issues = lint.issues
            if not lint.ok:
                validation_errors.extend(lint.issues)
                report.errors = validation_errors
                report.valid = False
            else:
                post_format_syntax = check_syntax(code, language)
                post_format_security = check_security(code, language)
                if not post_format_syntax.ok:
                    validation_errors.append(
                        f"Formatter output syntax error: {post_format_syntax.error}"
                    )
                    report.syntax_ok = False
                if not post_format_security.ok:
                    report.security_ok = False
                    report.security_issues = post_format_security.issues
                    validation_errors.extend(post_format_security.issues)

                if validation_errors:
                    report.errors = validation_errors
                    report.valid = False
                else:
                    # 4. Test collection check; this imports test modules, so it follows the security scan.
                    collection = run_collection_check(
                        test_code=code,
                        source_code=source_code,
                        module_name=module_name,
                        language=language,
                        framework=framework,
                    )
                    report.collect_ok = collection.ok
                    report.collection_executed = collection.executed
                    report.collect_output = collection.output
                    if not collection.executed:
                        report.warnings.append(collection.output)
                    if not collection.ok:
                        validation_errors.append(collection.output or "Test collection failed")
                        report.errors = validation_errors
                        report.valid = False
                    else:
                        # 5. Dry-run in Docker (or the explicitly configured local development runner).
                        if bool(options.get("run_sandbox_dry_run", True)):
                            dry = run_sandbox_dry_run(
                                test_code=code,
                                source_code=source_code,
                                module_name=module_name,
                                language=language,
                                framework=framework,
                            )
                            report.dry_run_results = dry
                            report.coverage = dry.coverage_pct
                            report.dry_run_ok = (
                                (dry.exit_code == 0 and dry.failed == 0) if dry.executed else True
                            )
                            if not dry.executed:
                                report.warnings.append(
                                    dry.stderr
                                    or "Sandbox dry-run did not execute; configure a Docker sandbox."
                                )
                            elif not report.dry_run_ok:
                                validation_errors.append(
                                    f"Sandbox dry-run failed (exit code {dry.exit_code}; "
                                    f"{dry.failed} failed of {dry.total} tests)."
                                )
                                if dry.stderr:
                                    validation_errors.append(dry.stderr)
                            coverage_target = int(options.get("coverage_target", 0) or 0)
                            if (
                                dry.executed
                                and coverage_target > 0
                                and dry.coverage_pct < coverage_target
                            ):
                                report.warnings.append(
                                    f"Coverage target is {coverage_target}%; observed {dry.coverage_pct}%."
                                )
                        else:
                            report.dry_run_results = DryRunResultSchema(
                                executed=False,
                                sandbox_backend="disabled-by-user",
                                exit_code=0,
                            )
                            report.dry_run_ok = True
                            report.warnings.append(
                                "Sandbox dry-run was disabled by the request options."
                            )

                        # Skipped execution is reported as a warning and never misrepresented as a pass.
                        dry_skipped_by_user = not bool(options.get("run_sandbox_dry_run", True))
                        dry_not_executed = not bool(report.dry_run_results.executed)
                        report.valid = (
                            report.syntax_ok
                            and report.security_ok
                            and report.lint_ok
                            and report.collect_ok
                            and (report.dry_run_ok or dry_skipped_by_user or dry_not_executed)
                        )
                        report.errors = validation_errors

    state_errors = list(state.get("errors") or [])
    result = {
        **state,
        "test_code": code,
        "validation_report": report.model_dump(mode="json"),
        "errors": state_errors + validation_errors,
    }
    attempts = int(state.get("iteration", 0))
    record_stage(
        result,
        "validating",
        started,
        input_summary=f"Validate {len(code)} chars; fix attempt={attempts}",
        output_summary=(
            "Validation passed"
            if report.valid
            else "Validation failed: " + "; ".join(validation_errors[:3])
        ),
        status="ok" if report.valid else "failed",
        errors=validation_errors,
        code_snapshot=code,
        current_stage="validating",
        progress=65 if report.valid else 50,
        run_status="running",
        updates={"test_code": code, "validation_report": report.model_dump(mode="json")},
    )
    return result
