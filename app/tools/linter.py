"""Syntax verification, security guard, auto-formatting (black/prettier), and linting (ruff/eslint)."""

from __future__ import annotations

import ast
import py_compile
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

FORBIDDEN_PYTHON_MODULES = {
    "subprocess",
    "socket",
    "pty",
    "ctypes",
    "multiprocessing",
}

FORBIDDEN_PYTHON_CALLS = {
    "eval",
    "exec",
    "compile",
    "__import__",
    "os.system",
    "os.popen",
    "os.spawnl",
    "os.spawnle",
    "os.spawnlp",
    "os.spawnlpe",
    "os.spawnv",
    "os.spawnve",
    "os.spawnvp",
    "os.spawnvpe",
    "os.execv",
    "os.execve",
    "os.execl",
    "os.fork",
    "subprocess.run",
    "subprocess.Popen",
    "subprocess.call",
    "subprocess.check_output",
    "subprocess.check_call",
    "socket.socket",
    "socket.create_connection",
}

ALLOWED_TMP_PREFIXES = ("/tmp", "/var/tmp", "tmp_path", "tmpdir", "tempfile")


@dataclass
class SecurityScanResult:
    """Result of scanning generated test code for dangerous patterns."""

    ok: bool = True
    issues: List[str] = field(default_factory=list)


@dataclass
class SyntaxCheckResult:
    """Result of syntax compilation check."""

    ok: bool = True
    error: Optional[str] = None


@dataclass
class LintFormatResult:
    """Result of auto-formatting and linting test code."""

    ok: bool = True
    formatted_code: str = ""
    issues: List[str] = field(default_factory=list)


def _get_call_qualname(node: ast.Call) -> str:
    """Extract dotted name from an ast.Call node."""
    try:
        return ast.unparse(node.func)
    except Exception:
        if isinstance(node.func, ast.Name):
            return node.func.id
        if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
            return f"{node.func.value.id}.{node.func.attr}"
        return ""


def _uses_temporary_root(node: ast.AST, safe_names: Optional[set[str]] = None) -> bool:
    """Return whether a path expression is statically rooted in a temporary directory."""
    safe_names = safe_names or set()
    for item in ast.walk(node):
        if isinstance(item, ast.Name) and item.id in {"tmp_path", "tmpdir", *safe_names}:
            return True
        if isinstance(item, ast.Call):
            name = _get_call_qualname(item)
            if name in {
                "tempfile.NamedTemporaryFile",
                "tempfile.TemporaryDirectory",
                "tempfile.mkstemp",
                "tempfile.mkdtemp",
            }:
                return True
            if (
                name in {"Path", "pathlib.Path"}
                and item.args
                and isinstance(item.args[0], ast.Constant)
            ):
                value = str(item.args[0].value)
                if value.startswith(("/tmp/", "/var/tmp/")):
                    return True
        if isinstance(item, ast.Constant) and isinstance(item.value, str):
            if item.value.startswith(("/tmp/", "/var/tmp/")):
                return True
    return False


def _literal_path(node: ast.AST) -> Optional[str]:
    """Return literal path argument when a call receives a literal string."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def check_security(code: str, language: str = "python") -> SecurityScanResult:
    """Inspect generated test code and reject process execution, network, dynamic execution,
    and filesystem operations that are not rooted in a temporary directory.

    This scan is a defense-in-depth policy gate. The default runner still uses isolated Docker;
    it is not a substitute for container isolation.
    """
    issues: List[str] = []
    lang = (language or "python").strip().lower()

    if lang == "python":
        try:
            tree = ast.parse(code)
        except SyntaxError:
            tree = None

        if tree is not None:
            blocked_modules = FORBIDDEN_PYTHON_MODULES | {"importlib", "runpy", "webbrowser"}
            safe_names = {"tmp_path", "tmpdir"}
            changed = True
            while changed:
                changed = False
                for node in ast.walk(tree):
                    if isinstance(node, (ast.Assign, ast.AnnAssign)):
                        value = node.value
                        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                        if value is not None and _uses_temporary_root(value, safe_names):
                            for target in targets:
                                if isinstance(target, ast.Name) and target.id not in safe_names:
                                    safe_names.add(target.id)
                                    changed = True

            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        top_mod = alias.name.split(".")[0]
                        if top_mod in blocked_modules:
                            issues.append(
                                f"Forbidden module import '{alias.name}' at line {node.lineno}"
                            )
                elif isinstance(node, ast.ImportFrom):
                    mod = (node.module or "").split(".")[0]
                    if mod in blocked_modules:
                        issues.append(
                            f"Forbidden from-import '{node.module}' at line {node.lineno}"
                        )
                elif isinstance(node, ast.Call):
                    call_name = _get_call_qualname(node)
                    if call_name in FORBIDDEN_PYTHON_CALLS or any(
                        call_name == mod or call_name.startswith(f"{mod}.")
                        for mod in blocked_modules
                    ):
                        issues.append(
                            f"Forbidden dangerous call '{call_name}()' at line {node.lineno}"
                        )
                    if any(
                        call_name == root or call_name.startswith(f"{root}.")
                        for root in {
                            "requests",
                            "httpx",
                            "urllib.request",
                            "aiohttp",
                            "websocket",
                            "websockets",
                        }
                    ):
                        issues.append(
                            f"Forbidden direct network call '{call_name}()' at line {node.lineno}; mock the dependency instead"
                        )

                    # Block OS process, environment and broad filesystem APIs.
                    forbidden_os_calls = {
                        "os.system",
                        "os.popen",
                        "os.fork",
                        "os.execv",
                        "os.execve",
                        "os.execl",
                        "os.spawnl",
                        "os.spawnle",
                        "os.spawnlp",
                        "os.spawnlpe",
                        "os.spawnv",
                        "os.spawnve",
                        "os.spawnvp",
                        "os.spawnvpe",
                        "os.remove",
                        "os.unlink",
                        "os.rmdir",
                        "os.removedirs",
                        "os.rename",
                        "os.replace",
                        "os.chmod",
                        "os.chown",
                        "os.truncate",
                        "os.open",
                        "os.write",
                        "os.listdir",
                        "os.scandir",
                        "os.walk",
                        "os.stat",
                        "os.lstat",
                        "os.getenv",
                        "os.putenv",
                        "os.unsetenv",
                        "os.chdir",
                        "os.mkdir",
                        "os.makedirs",
                        "os.path.exists",
                        "os.path.isfile",
                        "os.path.isdir",
                        "os.path.getsize",
                        "os.path.realpath",
                        "os.path.abspath",
                    }
                    if (
                        call_name in forbidden_os_calls
                        or call_name.startswith("os.spawn")
                        or call_name.startswith("os.exec")
                    ):
                        issues.append(
                            f"Forbidden OS/environment operation '{call_name}()' at line {node.lineno}"
                        )

                    # Built-in open is permitted only for paths visibly rooted in tmp_path/tmpdir/tempfile.
                    if call_name in {"open", "io.open", "builtins.open"}:
                        if not node.args or not _uses_temporary_root(node.args[0], safe_names):
                            literal = _literal_path(node.args[0]) if node.args else None
                            detail = (
                                f"'{literal}'"
                                if literal
                                else "a path not provably rooted in tmp_path"
                            )
                            issues.append(f"Forbidden file access {detail} at line {node.lineno}")
                        elif (
                            node.args
                            and _literal_path(node.args[0])
                            and ".." in (_literal_path(node.args[0]) or "")
                        ):
                            issues.append(f"Path traversal is forbidden at line {node.lineno}")

                    # Reject direct construction of absolute paths outside the test temp area.
                    if call_name in {"Path", "pathlib.Path"} and node.args:
                        literal = _literal_path(node.args[0])
                        if (
                            literal
                            and literal.startswith("/")
                            and not literal.startswith(("/tmp/", "/var/tmp/"))
                        ):
                            issues.append(
                                f"Forbidden absolute path '{literal}' at line {node.lineno}"
                            )
                        if literal and ".." in literal.split("/"):
                            issues.append(f"Path traversal in '{literal}' at line {node.lineno}")

                    # pathlib mutations/reads are allowed only for paths derived from tmp_path/tmpdir.
                    if isinstance(node.func, ast.Attribute) and node.func.attr in {
                        "open",
                        "read_text",
                        "read_bytes",
                        "write_text",
                        "write_bytes",
                        "unlink",
                        "rename",
                        "replace",
                        "mkdir",
                        "rmdir",
                        "touch",
                        "chmod",
                        "glob",
                        "rglob",
                    }:
                        receiver = node.func.value
                        if not _uses_temporary_root(receiver, safe_names):
                            # `Path(...)` itself is checked above; reject unknown receivers for file I/O.
                            if node.func.attr in {
                                "open",
                                "read_text",
                                "read_bytes",
                                "write_text",
                                "write_bytes",
                                "unlink",
                                "rename",
                                "replace",
                                "mkdir",
                                "rmdir",
                                "touch",
                                "chmod",
                            }:
                                issues.append(
                                    f"Filesystem operation '{node.func.attr}' is not proven to use tmp_path at line {node.lineno}"
                                )

    # Regex fallback checks across Python, JavaScript, and Java (also catches syntax-invalid code).
    dangerous_patterns: List[Tuple[str, str]] = [
        (r"\bos\.system\s*\(", "Forbidden call to os.system()"),
        (
            r"\bsubprocess\.(?:run|Popen|call|check_output|check_call)\s*\(",
            "Forbidden subprocess execution",
        ),
        (r"(?<![a-zA-Z0-9_.])eval\s*\(", "Forbidden call to eval()"),
        (r"(?<![a-zA-Z0-9_.])exec\s*\(", "Forbidden call to exec()"),
        (r"\bsocket\.socket\s*\(", "Forbidden raw socket creation"),
        (r"\brequire\s*\(\s*['\"]child_process['\"]", "Forbidden Node.js child_process import"),
        (r"\bfrom\s+['\"](?:node:)?child_process['\"]", "Forbidden Node.js child_process import"),
        (r"\b(?:eval|Function)\s*\(", "Forbidden JavaScript dynamic code execution"),
        (
            r"\b(?:fetch|XMLHttpRequest)\s*\(",
            "Forbidden JavaScript network call in generated tests",
        ),
        (r"Runtime\.getRuntime\(\)\.exec\s*\(", "Forbidden Java Runtime.exec() usage"),
        (r"ProcessBuilder\s*\(", "Forbidden Java ProcessBuilder usage"),
        (
            r"java\.net\.(?:Socket|URL|HttpURLConnection)",
            "Forbidden Java network API in generated tests",
        ),
    ]
    for pattern, description in dangerous_patterns:
        if re.search(pattern, code):
            if not any(description.lower() in existing.lower() for existing in issues):
                issues.append(description)

    return SecurityScanResult(ok=len(issues) == 0, issues=list(dict.fromkeys(issues)))


def check_syntax(code: str, language: str = "python") -> SyntaxCheckResult:
    """Verify syntax of generated test code (ast.parse + py_compile, node --check, etc.)."""
    lang = (language or "python").strip().lower()
    if not code or not code.strip():
        return SyntaxCheckResult(ok=False, error="Generated test code is empty")

    if lang == "python":
        try:
            ast.parse(code)
        except SyntaxError as exc:
            return SyntaxCheckResult(
                ok=False,
                error=f"SyntaxError at line {exc.lineno}, col {exc.offset}: {exc.msg}",
            )

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", encoding="utf-8", delete=False
        ) as tmp:
            tmp.write(code)
            tmp_path = Path(tmp.name)
        try:
            py_compile.compile(str(tmp_path), doraise=True)
            return SyntaxCheckResult(ok=True)
        except py_compile.PyCompileError as exc:
            return SyntaxCheckResult(ok=False, error=str(exc))
        finally:
            tmp_path.unlink(missing_ok=True)

    if lang in ("javascript", "js"):
        node_bin = shutil.which("node")
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".js", encoding="utf-8", delete=False
        ) as tmp:
            # Strip ES module import/export keywords only for node --check if CommonJS default
            sanitized_js = re.sub(
                r"^\s*import\s+.*?from\s+['\"].*?['\"];?\s*$",
                "// import statement",
                code,
                flags=re.MULTILINE,
            )
            sanitized_js = re.sub(r"^\s*export\s+", "", sanitized_js, flags=re.MULTILINE)
            tmp.write(sanitized_js)
            tmp_path = Path(tmp.name)
        try:
            if node_bin:
                proc = subprocess.run(
                    [node_bin, "--check", str(tmp_path)],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if proc.returncode != 0:
                    return SyntaxCheckResult(ok=False, error=(proc.stderr or proc.stdout).strip())
            return SyntaxCheckResult(ok=True)
        finally:
            tmp_path.unlink(missing_ok=True)

    if lang == "java":
        # Check balanced braces and class definition
        open_braces = code.count("{")
        close_braces = code.count("}")
        if open_braces != close_braces or open_braces == 0:
            return SyntaxCheckResult(
                ok=False,
                error=f"Unbalanced braces in Java test code: '{{'={open_braces}, '}}'={close_braces}",
            )
        if "class " not in code:
            return SyntaxCheckResult(
                ok=False,
                error="Java test code must contain a class declaration",
            )
        return SyntaxCheckResult(ok=True)

    return SyntaxCheckResult(ok=True)


def format_and_lint(code: str, language: str = "python") -> LintFormatResult:
    """Format code (Black for Python) and run static linter (Ruff for Python)."""
    lang = (language or "python").strip().lower()
    formatted = code
    issues: List[str] = []

    if lang == "python":
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", encoding="utf-8", delete=False
        ) as tmp:
            tmp.write(code)
            tmp_path = Path(tmp.name)

        try:
            # 1. Auto-format with Black
            black_bin = shutil.which("black")
            if black_bin:
                subprocess.run(
                    [black_bin, "--quiet", "--line-length", "100", str(tmp_path)],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )

            # 2. Run Ruff auto-fix for safe import/whitespace fixes, then check remaining issues
            ruff_bin = shutil.which("ruff")
            if ruff_bin:
                subprocess.run(
                    [
                        ruff_bin,
                        "check",
                        "--fix",
                        "--ignore",
                        "E501,F401",
                        str(tmp_path),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                check_proc = subprocess.run(
                    [
                        ruff_bin,
                        "check",
                        "--select",
                        "E9,F63,F7,F82",
                        str(tmp_path),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                if check_proc.returncode != 0:
                    for line in (check_proc.stdout or "").splitlines():
                        line = line.strip()
                        if line and not line.startswith("Found "):
                            issues.append(line)

            formatted = tmp_path.read_text(encoding="utf-8")
        except Exception as exc:
            issues.append(f"Linter warning: {exc}")
        finally:
            tmp_path.unlink(missing_ok=True)

        return LintFormatResult(
            ok=len(issues) == 0,
            formatted_code=formatted,
            issues=issues,
        )

    # For JS/Java: normalize trailing whitespace and ensure trailing newline
    lines = [ln.rstrip() for ln in code.splitlines()]
    formatted = "\n".join(lines).strip() + "\n"
    return LintFormatResult(ok=True, formatted_code=formatted, issues=issues)
