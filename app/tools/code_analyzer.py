"""Static code analyzer using Python's ast module and multi-language parsers.

Extracts functions, classes, method signatures, parameter types, defaults,
return annotations, docstrings, raised exceptions, conditional branches,
and external dependencies to ground LLM requirement parsing and test generation.
"""

from __future__ import annotations

import ast
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ParameterInfo:
    """Metadata for a single function/method parameter."""

    name: str
    annotation: Optional[str] = None
    default: Optional[str] = None
    kind: str = "positional_or_keyword"


@dataclass
class FunctionInfo:
    """Metadata extracted for a function or class method."""

    name: str
    qualname: str
    signature: str
    parameters: List[ParameterInfo] = field(default_factory=list)
    return_annotation: Optional[str] = None
    docstring: Optional[str] = None
    is_async: bool = False
    is_method: bool = False
    class_name: Optional[str] = None
    decorators: List[str] = field(default_factory=list)
    raised_exceptions: List[str] = field(default_factory=list)
    branches: List[str] = field(default_factory=list)
    called_functions: List[str] = field(default_factory=list)
    lineno: int = 1


@dataclass
class ClassInfo:
    """Metadata extracted for a class definition."""

    name: str
    bases: List[str] = field(default_factory=list)
    docstring: Optional[str] = None
    methods: List[FunctionInfo] = field(default_factory=list)
    init_signature: Optional[str] = None
    lineno: int = 1


@dataclass
class CodeAnalysisResult:
    """Full structural analysis result for a source file."""

    language: str
    syntax_valid: bool = True
    syntax_error: Optional[str] = None
    functions: List[FunctionInfo] = field(default_factory=list)
    classes: List[ClassInfo] = field(default_factory=list)
    imports: List[str] = field(default_factory=list)
    external_dependencies: List[str] = field(default_factory=list)
    constants: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize analysis result into a JSON-compatible dictionary."""
        return asdict(self)


STDLIB_OR_EXTERNAL_MOCK_CANDIDATES = {
    "requests",
    "httpx",
    "urllib",
    "aiohttp",
    "boto3",
    "redis",
    "psycopg2",
    "sqlalchemy",
    "pymongo",
    "sqlite3",
    "smtplib",
    "socket",
    "subprocess",
    "time",
    "datetime",
    "random",
    "os",
    "pathlib",
    "open",
    "fetch",
    "axios",
}


def _unparse_safe(node: Optional[ast.AST]) -> Optional[str]:
    """Safely unparse an AST node to source string."""
    if node is None:
        return None
    try:
        return ast.unparse(node)
    except Exception:
        return None


def _extract_python_function(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    class_name: Optional[str] = None,
) -> FunctionInfo:
    """Extract parameter signatures, docstrings, exceptions, and branches from a function AST node."""
    params: List[ParameterInfo] = []
    args = node.args

    # Positional-only + normal args
    posonly = getattr(args, "posonlyargs", [])
    all_pos = list(posonly) + list(args.args)
    num_defaults = len(args.defaults)
    default_offset = len(all_pos) - num_defaults

    for idx, arg in enumerate(all_pos):
        default_node = args.defaults[idx - default_offset] if idx >= default_offset else None
        kind = "positional_only" if idx < len(posonly) else "positional_or_keyword"
        params.append(
            ParameterInfo(
                name=arg.arg,
                annotation=_unparse_safe(arg.annotation),
                default=_unparse_safe(default_node),
                kind=kind,
            )
        )

    if args.vararg:
        params.append(
            ParameterInfo(
                name=f"*{args.vararg.arg}",
                annotation=_unparse_safe(args.vararg.annotation),
                kind="var_positional",
            )
        )

    for idx, kwarg in enumerate(args.kwonlyargs):
        kw_default = args.kw_defaults[idx] if idx < len(args.kw_defaults) else None
        params.append(
            ParameterInfo(
                name=kwarg.arg,
                annotation=_unparse_safe(kwarg.annotation),
                default=_unparse_safe(kw_default),
                kind="keyword_only",
            )
        )

    if args.kwarg:
        params.append(
            ParameterInfo(
                name=f"**{args.kwarg.arg}",
                annotation=_unparse_safe(args.kwarg.annotation),
                kind="var_keyword",
            )
        )

    return_ann = _unparse_safe(node.returns)

    # Build readable signature
    sig_parts: List[str] = []
    for p in params:
        piece = p.name
        if p.annotation:
            piece += f": {p.annotation}"
        if p.default is not None:
            piece += f" = {p.default}"
        sig_parts.append(piece)

    prefix = "async def " if isinstance(node, ast.AsyncFunctionDef) else "def "
    sig = f"{prefix}{node.name}({', '.join(sig_parts)})"
    if return_ann:
        sig += f" -> {return_ann}"

    docstring = ast.get_docstring(node)
    decorators = [d for d in (_unparse_safe(dec) for dec in node.decorator_list) if d]

    raised_exceptions: List[str] = []
    branches: List[str] = []
    called_functions: List[str] = []

    for child in ast.walk(node):
        if isinstance(child, ast.Raise) and child.exc is not None:
            if isinstance(child.exc, ast.Call):
                exc_name = _unparse_safe(child.exc.func)
            elif isinstance(child.exc, ast.Name):
                exc_name = child.exc.id
            else:
                exc_name = _unparse_safe(child.exc)
            if exc_name and exc_name not in raised_exceptions:
                raised_exceptions.append(exc_name)
        elif isinstance(child, ast.If):
            cond = _unparse_safe(child.test)
            if cond and cond not in branches:
                branches.append(f"if {cond}")
        elif isinstance(child, ast.Call):
            fn_name = _unparse_safe(child.func)
            if fn_name and fn_name not in called_functions:
                called_functions.append(fn_name)

    qualname = f"{class_name}.{node.name}" if class_name else node.name
    return FunctionInfo(
        name=node.name,
        qualname=qualname,
        signature=sig,
        parameters=params,
        return_annotation=return_ann,
        docstring=docstring,
        is_async=isinstance(node, ast.AsyncFunctionDef),
        is_method=class_name is not None,
        class_name=class_name,
        decorators=decorators,
        raised_exceptions=raised_exceptions,
        branches=branches,
        called_functions=called_functions,
        lineno=getattr(node, "lineno", 1),
    )


def analyze_python_code(source_code: str) -> CodeAnalysisResult:
    """Analyze Python source code using Python's built-in ast module."""
    result = CodeAnalysisResult(language="python")
    if not source_code or not source_code.strip():
        return result

    try:
        tree = ast.parse(source_code)
    except SyntaxError as exc:
        result.syntax_valid = False
        result.syntax_error = f"SyntaxError at line {exc.lineno}: {exc.msg}"
        return result

    imports: List[str] = []
    external_deps: List[str] = []
    constants: List[str] = []

    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)
                top_pkg = alias.name.split(".")[0]
                if top_pkg in STDLIB_OR_EXTERNAL_MOCK_CANDIDATES and top_pkg not in external_deps:
                    external_deps.append(top_pkg)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            for alias in node.names:
                full = f"{mod}.{alias.name}" if mod else alias.name
                imports.append(full)
            top_pkg = mod.split(".")[0] if mod else ""
            if top_pkg in STDLIB_OR_EXTERNAL_MOCK_CANDIDATES and top_pkg not in external_deps:
                external_deps.append(top_pkg)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fn_info = _extract_python_function(node)
            result.functions.append(fn_info)
            for call_name in fn_info.called_functions:
                root_call = call_name.split(".")[0]
                if (
                    root_call in STDLIB_OR_EXTERNAL_MOCK_CANDIDATES
                    and root_call not in external_deps
                ):
                    external_deps.append(root_call)
        elif isinstance(node, ast.ClassDef):
            bases = [b for b in (_unparse_safe(base) for base in node.bases) if b]
            methods: List[FunctionInfo] = []
            init_sig: Optional[str] = None
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    m_info = _extract_python_function(item, class_name=node.name)
                    methods.append(m_info)
                    if item.name == "__init__":
                        init_sig = m_info.signature
                    for call_name in m_info.called_functions:
                        root_call = call_name.split(".")[0]
                        if (
                            root_call in STDLIB_OR_EXTERNAL_MOCK_CANDIDATES
                            and root_call not in external_deps
                        ):
                            external_deps.append(root_call)
            result.classes.append(
                ClassInfo(
                    name=node.name,
                    bases=bases,
                    docstring=ast.get_docstring(node),
                    methods=methods,
                    init_signature=init_sig,
                    lineno=getattr(node, "lineno", 1),
                )
            )
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.isupper():
                    constants.append(target.id)

    result.imports = imports
    result.external_dependencies = external_deps
    result.constants = constants
    return result


def analyze_javascript_code(source_code: str) -> CodeAnalysisResult:
    """Analyze JavaScript/TypeScript source code for functions, classes, branches, and throws."""
    result = CodeAnalysisResult(language="javascript")
    if not source_code or not source_code.strip():
        return result

    # Match standard function declarations: [async] function name(a, b = 1)
    fn_pattern = re.compile(
        r"(?:export\s+)?(?P<async>async\s+)?function\s+(?P<name>[a-zA-Z_$][a-zA-Z0-9_$]*)\s*\((?P<params>[^)]*)\)",
        re.MULTILINE,
    )
    # Match arrow functions: const name = [async] (a, b) =>
    arrow_pattern = re.compile(
        r"(?:export\s+)?(?:const|let|var)\s+(?P<name>[a-zA-Z_$][a-zA-Z0-9_$]*)\s*=\s*(?P<async>async\s+)?\((?P<params>[^)]*)\)\s*=>",
        re.MULTILINE,
    )
    throw_pattern = re.compile(r"throw\s+new\s+([a-zA-Z0-9_$]+)")
    if_pattern = re.compile(r"if\s*\(([^)]+)\)")

    throws = list(dict.fromkeys(throw_pattern.findall(source_code)))
    branches = [f"if ({cond.strip()})" for cond in if_pattern.findall(source_code)[:10]]

    seen_names = set()
    for match in list(fn_pattern.finditer(source_code)) + list(arrow_pattern.finditer(source_code)):
        name = match.group("name")
        if name in seen_names:
            continue
        seen_names.add(name)
        raw_params = match.group("params").strip()
        param_list: List[ParameterInfo] = []
        if raw_params:
            for part in raw_params.split(","):
                part = part.strip()
                if not part:
                    continue
                if "=" in part:
                    p_name, p_def = [x.strip() for x in part.split("=", 1)]
                    param_list.append(ParameterInfo(name=p_name, default=p_def))
                else:
                    param_list.append(ParameterInfo(name=part))
        is_async = bool(match.group("async"))
        sig = f"{'async ' if is_async else ''}function {name}({raw_params})"
        result.functions.append(
            FunctionInfo(
                name=name,
                qualname=name,
                signature=sig,
                parameters=param_list,
                is_async=is_async,
                raised_exceptions=throws,
                branches=branches,
            )
        )

    class_pattern = re.compile(
        r"class\s+(?P<name>[a-zA-Z_$][a-zA-Z0-9_$]*)(?:\s+extends\s+(?P<base>[a-zA-Z_$][a-zA-Z0-9_$]*))?"
    )
    for cm in class_pattern.finditer(source_code):
        c_name = cm.group("name")
        base = cm.group("base")
        result.classes.append(
            ClassInfo(
                name=c_name,
                bases=[base] if base else [],
            )
        )

    for dep in ("fetch", "axios", "fs", "http", "https"):
        if re.search(rf"\b{dep}\b", source_code):
            result.external_dependencies.append(dep)

    return result


def analyze_java_code(source_code: str) -> CodeAnalysisResult:
    """Analyze Java source code for classes, methods, exceptions, and branches."""
    result = CodeAnalysisResult(language="java")
    if not source_code or not source_code.strip():
        return result

    class_pattern = re.compile(
        r"(?:public|private|protected)?\s*class\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
    )
    method_pattern = re.compile(
        r"(?:public|protected|private)\s+(?:static\s+)?(?P<ret>[A-Za-z0-9_<>\[\]]+)\s+"
        r"(?P<name>[a-zA-Z_][a-zA-Z0-9_]*)\s*\((?P<params>[^)]*)\)"
    )
    throw_pattern = re.compile(r"throw\s+new\s+([A-Za-z0-9_]+)")
    if_pattern = re.compile(r"if\s*\(([^)]+)\)")

    throws = list(dict.fromkeys(throw_pattern.findall(source_code)))
    branches = [f"if ({cond.strip()})" for cond in if_pattern.findall(source_code)[:10]]

    classes: List[ClassInfo] = []
    class_names = [m.group("name") for m in class_pattern.finditer(source_code)]
    primary_class = class_names[0] if class_names else None

    methods: List[FunctionInfo] = []
    for mm in method_pattern.finditer(source_code):
        m_name = mm.group("name")
        ret = mm.group("ret")
        raw_params = mm.group("params").strip()
        if m_name in class_names:
            continue
        param_list: List[ParameterInfo] = []
        if raw_params:
            for p in raw_params.split(","):
                tokens = p.strip().split()
                if len(tokens) >= 2:
                    param_list.append(
                        ParameterInfo(name=tokens[-1], annotation=" ".join(tokens[:-1]))
                    )
        fn_info = FunctionInfo(
            name=m_name,
            qualname=f"{primary_class}.{m_name}" if primary_class else m_name,
            signature=f"public {ret} {m_name}({raw_params})",
            parameters=param_list,
            return_annotation=ret,
            is_method=primary_class is not None,
            class_name=primary_class,
            raised_exceptions=throws,
            branches=branches,
        )
        methods.append(fn_info)
        result.functions.append(fn_info)

    for c_name in class_names:
        classes.append(ClassInfo(name=c_name, methods=methods))
    result.classes = classes
    return result


def analyze_source_code(source_code: Optional[str], language: str = "python") -> CodeAnalysisResult:
    """Dispatch code analysis based on language ('python', 'javascript', 'java')."""
    lang = (language or "python").strip().lower()
    if not source_code or not source_code.strip():
        return CodeAnalysisResult(language=lang)
    if lang == "python":
        return analyze_python_code(source_code)
    if lang in ("javascript", "js", "typescript", "ts"):
        return analyze_javascript_code(source_code)
    if lang == "java":
        return analyze_java_code(source_code)
    return analyze_python_code(source_code)
