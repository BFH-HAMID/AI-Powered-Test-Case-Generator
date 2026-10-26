"""Test generation node for pytest/unittest, Jest, and JUnit using LLM or deterministic offline mode."""

from __future__ import annotations

import ast
import re
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from langchain_core.messages import HumanMessage, SystemMessage

from app.agent.llm import get_chat_model
from app.agent.nodes._common import json_safe, record_stage
from app.agent.prompts import TEST_GENERATION_SYSTEM_PROMPT, generation_prompt
from app.core.logging import get_logger

logger = get_logger(__name__)


def extract_code_block(response: Any) -> str:
    """Extract the first fenced code block, or return stripped model text."""
    content = getattr(response, "content", response)
    if isinstance(content, list):
        text = "\n".join(
            str(item.get("text", "")) if isinstance(item, dict) else str(item) for item in content
        )
    else:
        text = str(content or "")
    match = re.search(r"```(?:[a-zA-Z0-9_+-]+)?\s*\n?(.*?)```", text, re.DOTALL)
    return (match.group(1) if match else text).strip() + "\n"


def _safe_annotation_value(annotation: Optional[str], default: Optional[str]) -> str:
    """Choose a harmless representative parameter value for a local smoke test."""
    if default is not None and default not in {"...", "Ellipsis"}:
        try:
            value = ast.literal_eval(default)
            return repr(value)
        except Exception:
            if default in {"None", "True", "False"}:
                return default
    ann = (annotation or "").replace("typing.", "").lower()
    if "optional" in ann or "none" in ann or "union" in ann:
        return "None"
    if "bool" in ann:
        return "True"
    if "int" in ann or "long" in ann:
        return "1"
    if "float" in ann or "decimal" in ann:
        return "1.0"
    if "str" in ann or "char" in ann or "string" in ann:
        return '"sample"'
    if "dict" in ann or "map" in ann:
        return "{}"
    if "list" in ann or "sequence" in ann or "tuple" in ann or "set" in ann:
        return "[]"
    return '"sample"'


def _param_entries(function: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return required call arguments for an analyzed function/method."""
    result = []
    for param in function.get("parameters", []):
        name = str(param.get("name", ""))
        if name in {"self", "cls"} or name.startswith("*"):
            continue
        if param.get("default") is None:
            result.append(param)
    return result


def _static_evaluate(node: ast.AST, values: Dict[str, Any]) -> Any:
    """Evaluate a deliberately small, side-effect-free AST expression subset."""
    binary_ops = {
        ast.Add: lambda a, b: a + b,
        ast.Sub: lambda a, b: a - b,
        ast.Mult: lambda a, b: a * b,
        ast.Div: lambda a, b: a / b,
        ast.FloorDiv: lambda a, b: a // b,
        ast.Mod: lambda a, b: a % b,
        ast.Pow: lambda a, b: (
            a ** b if abs(b) <= 8 else (_ for _ in ()).throw(ValueError("large exponent"))
        ),
    }
    unary_ops = {ast.UAdd: lambda x: +x, ast.USub: lambda x: -x, ast.Not: lambda x: not x}
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in values:
            return values[node.id]
        raise ValueError(f"unknown value {node.id}")
    if isinstance(node, ast.BinOp) and type(node.op) in binary_ops:
        return binary_ops[type(node.op)](
            _static_evaluate(node.left, values), _static_evaluate(node.right, values)
        )
    if isinstance(node, ast.UnaryOp) and type(node.op) in unary_ops:
        return unary_ops[type(node.op)](_static_evaluate(node.operand, values))
    if isinstance(node, ast.BoolOp):
        evaluated = [_static_evaluate(item, values) for item in node.values]
        return all(evaluated) if isinstance(node.op, ast.And) else any(evaluated)
    if isinstance(node, ast.IfExp):
        test = _static_evaluate(node.test, values)
        return _static_evaluate(node.body if test else node.orelse, values)
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        elements = [_static_evaluate(item, values) for item in node.elts]
        return (
            elements
            if isinstance(node, ast.List)
            else (tuple(elements) if isinstance(node, ast.Tuple) else set(elements))
        )
    if isinstance(node, ast.Dict):
        return {
            _static_evaluate(key, values): _static_evaluate(value, values)
            for key, value in zip(node.keys, node.values)
            if key is not None
        }
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        safe_functions = {
            "abs": abs,
            "len": len,
            "min": min,
            "max": max,
            "round": round,
            "sum": sum,
        }
        if node.func.id in safe_functions:
            args = [_static_evaluate(arg, values) for arg in node.args]
            return safe_functions[node.func.id](*args)
    raise ValueError("expression is outside the safe static evaluator")


def _infer_static_return_value(
    source_code: Optional[str],
    function: Dict[str, Any],
    class_name: Optional[str],
    call_args: List[str],
) -> tuple[bool, Any]:
    """Infer an expected literal for a simple single-return function without executing source."""
    if not source_code:
        return False, None
    try:
        tree = ast.parse(source_code)
    except SyntaxError:
        return False, None
    target_name = str(function.get("name", "")).split(".")[-1]
    candidates: List[tuple[str | None, ast.FunctionDef | ast.AsyncFunctionDef]] = []

    def visit(node: ast.AST, owner: str | None = None) -> None:
        if isinstance(node, ast.ClassDef):
            for child in node.body:
                visit(child, node.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            candidates.append((owner, node))

    for item in tree.body:
        visit(item)
    matched = next(
        (
            node
            for owner, node in candidates
            if node.name == target_name
            and (owner == class_name or (owner is None and class_name is None))
        ),
        None,
    )
    if matched is None:
        return False, None
    body = list(matched.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body.pop(0)
    if len(body) != 1 or not isinstance(body[0], ast.Return) or body[0].value is None:
        return False, None

    values: Dict[str, Any] = {}
    arguments = list(matched.args.posonlyargs) + list(matched.args.args)
    if class_name and arguments and arguments[0].arg in {"self", "cls"}:
        arguments = arguments[1:]
    for arg, raw_value in zip(arguments, call_args):
        try:
            values[arg.arg] = ast.literal_eval(raw_value)
        except Exception:
            return False, None
    defaults = list(matched.args.defaults)
    original_args = list(matched.args.posonlyargs) + list(matched.args.args)
    original_offset = len(original_args) - len(defaults)
    for index, arg in enumerate(original_args):
        if arg.arg in {"self", "cls"} or arg.arg in values or index < original_offset:
            continue
        try:
            values[arg.arg] = _static_evaluate(defaults[index - original_offset], values)
        except Exception:
            return False, None
    try:
        return True, _static_evaluate(body[0].value, values)
    except Exception:
        return False, None


def _function_map(analysis: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Index top-level functions and class methods by qualified name."""
    index: Dict[str, Dict[str, Any]] = {}
    for fn in analysis.get("functions", []):
        index[fn.get("name", "")] = fn
    for cls in analysis.get("classes", []):
        for fn in cls.get("methods", []):
            index[fn.get("qualname") or f"{cls.get('name')}.{fn.get('name')}"] = fn
            index.setdefault(fn.get("name", ""), fn)
    return index


def _external_patch_calls(
    function: Dict[str, Any], imports: Sequence[str], module_name: str
) -> List[str]:
    """Build conservative unittest.mock patch targets for imported dependency calls."""
    imported_roots = {str(item).split(".")[0] for item in imports}
    external_roots = {"requests", "httpx", "urllib", "aiohttp", "boto3", "redis", "time", "random"}
    targets: List[str] = []
    for called in function.get("called_functions", []):
        if not isinstance(called, str) or "." not in called:
            continue
        root = called.split(".", 1)[0]
        if root in imported_roots and root in external_roots and called not in targets:
            targets.append(f"{module_name}.{called}")
    return targets


def _python_test_code(state: Dict[str, Any]) -> str:
    """Build deterministic pytest/unittest smoke tests grounded in source signatures."""
    spec = state.get("parsed_spec") or {}
    analysis = state.get("code_analysis") or {}
    module_name = str(state.get("module_name") or "target_module")
    framework = str(state.get("framework") or "pytest").lower()
    functions = spec.get("functions_under_test", [])
    index = _function_map(analysis)
    imports = analysis.get("imports", [])
    source_code = state.get("source_code")

    lines: List[str] = []
    if framework == "unittest":
        lines.extend(
            [
                "import unittest",
                "from unittest.mock import patch",
                f"import {module_name} as target",
                "",
            ]
        )
    else:
        lines.extend(["from unittest.mock import patch", f"import {module_name} as target", ""])

    case_specs: List[Tuple[str, Dict[str, Any], Optional[str]]] = []
    for fn in functions:
        name = str(fn.get("name", "")).strip()
        if not name:
            continue
        found = index.get(name) or index.get(name.split(".")[-1])
        case_specs.append((name, found or {}, name.rsplit(".", 1)[0] if "." in name else None))

    if not case_specs:
        if framework == "unittest":
            lines.extend(
                [
                    "class TestModuleImport(unittest.TestCase):",
                    "    def test_target_module_is_importable(self):",
                    '        """Confirm the requested implementation module can be imported."""',
                    "        self.assertIsNotNone(target)",
                    "",
                ]
            )
        else:
            lines.extend(
                [
                    "def test_target_module_is_importable():",
                    '    """Confirm the requested implementation module can be imported."""',
                    "    assert target is not None",
                    "",
                ]
            )
        return "\n".join(lines).rstrip() + "\n"

    for ordinal, (spec_name, fn, class_name) in enumerate(case_specs, start=1):
        function_name = spec_name.split(".")[-1]
        simple_name = re.sub(r"[^a-zA-Z0-9_]+", "_", function_name).strip("_").lower()
        test_name = f"test_{simple_name}_smoke_{ordinal}"
        args = [
            _safe_annotation_value(p.get("annotation"), p.get("default"))
            for p in _param_entries(fn)
        ]
        targets = _external_patch_calls(fn, imports, module_name)
        has_expected, expected_value = _infer_static_return_value(source_code, fn, class_name, args)

        if class_name:
            if framework == "unittest":
                lines.append(
                    f"class Test{simple_name.title().replace('_', '')}(unittest.TestCase):"
                )
                lines.append(f"    def {test_name}(self):")
            else:
                lines.append(f"def {test_name}():")
            lines.append(
                f'    """Smoke-test {spec_name} without assuming unspecified return values."""'
            )
            class_token = class_name.split(".")[0]
            constructor = index.get(f"{class_token}.__init__") or index.get("__init__") or {}
            constructor_args = [
                _safe_annotation_value(p.get("annotation"), p.get("default"))
                for p in _param_entries(constructor)
            ]
            lines.append(f"    instance = target.{class_token}({', '.join(constructor_args)})")
            call_target = f"instance.{function_name}"
        else:
            if framework == "unittest":
                class_label = "Test" + simple_name.title().replace("_", "")
                lines.append(f"class {class_label}(unittest.TestCase):")
                lines.append(f"    def {test_name}(self):")
            else:
                lines.append(f"def {test_name}():")
            lines.append(
                f'    """Smoke-test {spec_name} without assuming unspecified return values."""'
            )
            call_target = f"target.{function_name}"

        indent = "    "
        if targets:
            lines.append(
                "    # Patch imported external boundaries; the sandbox blocks network access."
            )
            for patch_target in targets:
                lines.append(f"{indent}with patch({patch_target!r}):")
                indent += "    "

        call_expression = f"{call_target}({', '.join(args)})"
        if fn.get("is_async"):
            if "import asyncio" not in lines:
                lines.insert(0, "import asyncio")
            call_expression = f"asyncio.run({call_expression})"

        if has_expected:
            lines.append(f"{indent}actual = {call_expression}")
            if framework == "unittest":
                lines.append(f"{indent}self.assertEqual(actual, {expected_value!r})")
            else:
                lines.append(f"{indent}assert actual == {expected_value!r}")
        else:
            lines.append(f"{indent}{call_expression}")
        if framework == "unittest":
            lines.append("")
        lines.append("")

    if framework == "unittest":
        lines.extend(["if __name__ == '__main__':", "    unittest.main()"])
    return "\n".join(lines).rstrip() + "\n"


def _javascript_test_code(state: Dict[str, Any]) -> str:
    """Create a conservative Jest test file for JavaScript source analysis."""
    module_name = str(state.get("module_name") or "target_module").replace("\\", "/")
    module_path = module_name if module_name.startswith(".") else f"./{module_name}"
    analysis = state.get("code_analysis") or {}
    functions = analysis.get("functions", [])
    lines = [
        f'const target = require("{module_path}");',
        "",
        "describe('generated smoke tests', () => {",
    ]
    if not functions:
        lines.extend(
            [
                "  test('implementation module can be loaded', () => {",
                "    expect(target).toBeDefined();",
                "  });",
            ]
        )
    for fn in functions:
        name = fn.get("name")
        if not name:
            continue
        args = []
        for param in _param_entries(fn):
            args.append(_safe_annotation_value(param.get("annotation"), param.get("default")))
        lines.extend(
            [
                f"  test('{name} is callable and runs for a representative input', () => {{",
                f"    expect(typeof target.{name}).toBe('function');",
                f"    target.{name}({', '.join(args)});",
                "  });",
            ]
        )
    lines.extend(["});", ""])
    return "\n".join(lines)


def _java_test_code(state: Dict[str, Any]) -> str:
    """Create a JUnit 5 class-load test for the analyzed source class."""
    analysis = state.get("code_analysis") or {}
    classes = analysis.get("classes", [])
    module_name = str(state.get("module_name") or "Target")
    class_name = classes[0].get("name") if classes else module_name.rsplit(".", 1)[-1]
    source_code = state.get("source_code") or ""
    package_match = re.search(
        r"^\s*package\s+([A-Za-z_][A-Za-z0-9_.]*)\s*;", source_code, re.MULTILINE
    )
    package_name = package_match.group(1) if package_match else ""
    qualified_name = f"{package_name}.{class_name}" if package_name else class_name
    test_class = f"{class_name}GeneratedTest"
    lines = []
    if package_name:
        lines.extend([f"package {package_name};", ""])
    lines.extend(
        [
            "import static org.junit.jupiter.api.Assertions.assertNotNull;",
            "import org.junit.jupiter.api.Test;",
            "",
            f"class {test_class} {{",
            "    @Test",
            "    void implementationClassCanBeLoaded() throws ClassNotFoundException {",
            f'        assertNotNull(Class.forName("{qualified_name}"));',
            "    }",
            "}",
        ]
    )
    return "\n".join(lines) + "\n"


def local_generate_tests(state: Dict[str, Any]) -> str:
    """Generate conservative offline tests when no remote LLM is configured."""
    language = str(state.get("language") or "python").lower()
    if language == "python":
        return _python_test_code(state)
    if language in {"javascript", "js"}:
        return _javascript_test_code(state)
    if language == "java":
        return _java_test_code(state)
    raise ValueError(f"Unsupported language '{language}'")


def generate_tests_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """LangGraph node: generate test code with configured LLM or local deterministic fallback."""
    started = time.perf_counter()
    options = state.get("options") or {}
    language = str(state.get("language") or "python")
    framework = str(state.get("framework") or "pytest")
    module_name = str(state.get("module_name") or "target_module")
    requirement = str(state.get("requirement") or "")
    parsed_spec = state.get("parsed_spec") or {}
    code_analysis = state.get("code_analysis") or {}
    generation_errors: List[str] = []

    chat_model = get_chat_model(options.get("model"))
    generation_mode = "local-synthesizer"
    if chat_model is None:
        code = local_generate_tests(state)
    else:
        try:
            response = chat_model.invoke(
                [
                    SystemMessage(content=TEST_GENERATION_SYSTEM_PROMPT),
                    HumanMessage(
                        content=generation_prompt(
                            requirement=requirement,
                            parsed_spec_json=json_safe(parsed_spec),
                            source_analysis_json=json_safe(code_analysis),
                            language=language,
                            framework=framework,
                            module_name=module_name,
                        )
                    ),
                ]
            )
            code = extract_code_block(response)
            if not code.strip():
                raise ValueError("Generation model returned empty test code")
            generation_mode = "configured-llm"
        except Exception as exc:
            logger.exception(
                "LLM test generation failed; falling back to local synthesizer",
                extra={"run_id": state.get("run_id"), "stage": "generating"},
            )
            generation_errors.append(
                f"LLM test generation failed; deterministic local tests were used: {exc}"
            )
            code = local_generate_tests(state)
            generation_mode = "local-synthesizer-fallback"

    result = {
        **state,
        "test_code": code,
        "original_test_code": str(state.get("original_test_code") or code),
        "errors": list(state.get("errors") or []) + generation_errors,
    }
    record_stage(
        result,
        "generating",
        started,
        input_summary=(
            f"Generate {language}/{framework} tests for module {module_name}; "
            f"{len(parsed_spec.get('functions_under_test') or [])} target(s)"
        ),
        output_summary=(f"Generated {len(code.splitlines())} line(s) using {generation_mode}"),
        status="ok",
        errors=generation_errors,
        code_snapshot=code,
        current_stage="generating",
        progress=30,
        run_status="running",
        updates={"test_code": code},
    )
    return result
