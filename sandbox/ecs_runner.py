"""Fargate one-shot test runner; test contents arrive through short-lived S3 presigned URLs."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Tuple

import httpx

NETWORK_BLOCK = """import socket as _socket
_orig_socket = _socket.socket
class _BlockedSocket(_orig_socket):
    def connect(self, *args, **kwargs):
        raise PermissionError("Sandbox network access is disabled")
    def connect_ex(self, *args, **kwargs):
        raise PermissionError("Sandbox network access is disabled")
_socket.socket = _BlockedSocket
def _blocked(*args, **kwargs):
    raise PermissionError("Sandbox network access is disabled")
_socket.create_connection = _blocked
"""


def _module_file(workspace: Path, module_name: str, source_code: str) -> Path:
    """Write module source into workspace package structure and make it read-only."""
    parts = [part for part in re.split(r"[./]", module_name.strip("./")) if part]
    parts = [re.sub(r"[^A-Za-z0-9_]", "_", part) for part in parts] or ["target_module"]
    current = workspace
    for part in parts[:-1]:
        current = current / part
        current.mkdir(parents=True, exist_ok=True)
        (current / "__init__.py").write_text("", encoding="utf-8")
    target = current / f"{parts[-1]}.py"
    target.write_text(source_code or "# No source code supplied.\n", encoding="utf-8")
    target.chmod(0o444)
    return target


def _parse_junit(path: Path) -> Tuple[int, int, int, int, float, List[Dict[str, Any]]]:
    """Parse pytest JUnit XML into a result tuple."""
    if not path.exists():
        return 0, 0, 0, 0, 0.0, []
    root = ET.parse(path).getroot()
    cases: List[Dict[str, Any]] = []
    total = passed = failed = skipped = 0
    duration_ms = 0.0
    for item in root.findall(".//testcase"):
        total += 1
        name = item.attrib.get("name", f"test_{total}")
        seconds = float(item.attrib.get("time", "0") or 0)
        ms = round(seconds * 1000.0, 2)
        duration_ms += ms
        failure = item.find("failure")
        error = item.find("error")
        skipped_item = item.find("skipped")
        if failure is not None:
            failed += 1
            status = "failed"
            message = failure.attrib.get("message") or (failure.text or "").strip()
        elif error is not None:
            failed += 1
            status = "error"
            message = error.attrib.get("message") or (error.text or "").strip()
        elif skipped_item is not None:
            skipped += 1
            status = "skipped"
            message = skipped_item.attrib.get("message") or None
        else:
            passed += 1
            status = "passed"
            message = None
        cases.append(
            {
                "name": name,
                "classname": item.attrib.get("classname"),
                "status": status,
                "marker": "✅" if status == "passed" else ("⏭️" if status == "skipped" else "❌"),
                "duration_ms": ms,
                "message": (message or "")[:1000] or None,
            }
        )
    return total, passed, failed, skipped, round(duration_ms, 2), cases


def _drop_privileges() -> None:
    """Run generated test code as an unprivileged UID distinct from the S3 URL-owning runner."""
    if os.geteuid() == 0:
        os.setgroups([])
        os.setgid(10002)
        os.setuid(10002)


def _coverage_percent(path: Path) -> float:
    """Read Cobertura line-rate coverage report."""
    if not path.exists():
        return 0.0
    try:
        root = ET.parse(path).getroot()
        return round(float(root.attrib.get("line-rate", 0.0)) * 100.0, 2)
    except (ET.ParseError, ValueError):
        return 0.0


def main() -> int:
    """Download one run input, execute pytest with a scrubbed environment, upload JSON output."""
    input_url = os.environ.get("SANDBOX_INPUT_URL", "")
    output_url = os.environ.get("SANDBOX_OUTPUT_URL", "")
    run_id = os.environ.get("SANDBOX_RUN_ID", "unknown")
    timeout = int(os.environ.get("SANDBOX_TIMEOUT_SECONDS", "120"))
    if not input_url or not output_url:
        print("Missing one-shot S3 input/output URLs", file=sys.stderr)
        return 2

    try:
        response = httpx.get(input_url, timeout=30.0)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        print(f"Unable to download sandbox input: {exc}", file=sys.stderr)
        return 2

    output: Dict[str, Any] = {
        "executed": True,
        "sandbox_backend": "aws-ecs-fargate",
        "exit_code": 1,
        "total": 0,
        "passed": 0,
        "failed": 0,
        "skipped": 0,
        "duration_ms": 0.0,
        "coverage_pct": 0.0,
        "stdout": "",
        "stderr": "",
        "test_cases": [],
    }
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="fargate-test-") as tmp:
        workspace = Path(tmp) / "workspace"
        results = Path(tmp) / "results"
        workspace.mkdir()
        results.mkdir()
        results.chmod(0o777)
        module_name = str(payload.get("module_name") or "target_module")
        source = str(payload.get("source_code") or "")
        language = str(payload.get("language") or "python").lower()
        if language != "python":
            output["executed"] = False
            output["sandbox_backend"] = "fargate-python-only"
            output["stderr"] = (
                "Fargate image currently executes Python/pytest only; use the GitHub Actions JS/Java job."
            )
        else:
            _module_file(workspace, module_name, source)
            (workspace / "sitecustomize.py").write_text(NETWORK_BLOCK, encoding="utf-8")
            test_file = workspace / "test_generated.py"
            test_file.write_text(str(payload.get("test_code") or ""), encoding="utf-8")
            test_file.chmod(0o444)
            workspace.chmod(0o555)
            junit = results / "junit.xml"
            coverage = results / "coverage.xml"
            command = [
                sys.executable,
                "-m",
                "pytest",
                str(test_file),
                "-q",
                "--tb=short",
                "-p",
                "no:cacheprovider",
                f"--junitxml={junit}",
            ]
            if source.strip():
                module_root = module_name.split(".")[0].split("/")[0]
                command.extend([f"--cov={module_root}", f"--cov-report=xml:{coverage}"])
            safe_env = {
                "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                "PYTHONPATH": str(workspace),
                "PYTHONDONTWRITEBYTECODE": "1",
                "HOME": str(results),
                "TMPDIR": "/tmp",
                "COVERAGE_FILE": str(results / ".coverage"),
            }
            try:
                proc = subprocess.run(
                    command,
                    cwd=str(workspace),
                    env=safe_env,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    preexec_fn=_drop_privileges,
                )
                total, passed, failed, skipped, junit_duration, test_cases = _parse_junit(junit)
                output.update(
                    {
                        "exit_code": proc.returncode,
                        "total": total,
                        "passed": passed,
                        "failed": failed,
                        "skipped": skipped,
                        "duration_ms": junit_duration
                        or round((time.perf_counter() - started) * 1000.0, 2),
                        "coverage_pct": _coverage_percent(coverage),
                        "stdout": (proc.stdout or "")[:10000],
                        "stderr": (proc.stderr or "")[:10000],
                        "test_cases": test_cases,
                    }
                )
            except subprocess.TimeoutExpired as exc:
                output.update(
                    {
                        "exit_code": 124,
                        "failed": 1,
                        "duration_ms": round((time.perf_counter() - started) * 1000.0, 2),
                        "stderr": f"Sandbox test run exceeded {timeout} seconds: {exc}",
                        "test_cases": [
                            {
                                "name": "sandbox_timeout",
                                "status": "error",
                                "marker": "❌",
                                "duration_ms": timeout * 1000,
                                "message": f"Timed out after {timeout} seconds",
                            }
                        ],
                    }
                )

    output["duration_ms"] = output.get("duration_ms") or round(
        (time.perf_counter() - started) * 1000.0, 2
    )
    try:
        uploaded = httpx.put(output_url, json=output, timeout=30.0)
        uploaded.raise_for_status()
    except Exception as exc:
        print(f"Unable to upload sandbox result for {run_id}: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps({"run_id": run_id, "exit_code": output["exit_code"], "total": output["total"]})
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
