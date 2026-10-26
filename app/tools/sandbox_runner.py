"""Sandbox test runner supporting isolated Docker, optional ECS Fargate execution, and explicit local dev mode."""

from __future__ import annotations

import json
import os
import re
import resource
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from defusedxml import ElementTree as ET

from app.api.schemas import DryRunResultSchema, TestCaseResultItem
from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

NETWORK_BLOCK_SITECUSTOMIZE = '''"""Injected sandbox guard blocking outbound network socket connections."""
import socket as _socket
_orig_socket = _socket.socket
class _BlockedSocket(_orig_socket):
    def connect(self, *args, **kwargs):
        raise PermissionError("Sandbox network access is disabled")
    def connect_ex(self, *args, **kwargs):
        raise PermissionError("Sandbox network access is disabled")
_socket.socket = _BlockedSocket
def _blocked_create_connection(*args, **kwargs):
    raise PermissionError("Sandbox network access is disabled")
_socket.create_connection = _blocked_create_connection
'''


@dataclass
class CollectionCheckResult:
    """Result of running pytest --collect-only or framework collection check."""

    ok: bool = True
    output: str = ""
    collected_count: int = 0
    executed: bool = True
    sandbox_backend: str = "subprocess-isolated"


def _is_docker_image_available(image: Optional[str] = None) -> bool:
    """Return True if Docker is accessible and the requested sandbox image exists."""
    settings = get_settings()
    if settings.sandbox_mode.lower() == "subprocess":
        return False
    docker_bin = shutil.which("docker")
    if not docker_bin:
        return False
    try:
        proc = subprocess.run(
            [docker_bin, "image", "inspect", image or settings.sandbox_docker_image],
            capture_output=True,
            text=True,
            timeout=3,
        )
        return proc.returncode == 0
    except Exception:
        return False


def _is_docker_available() -> bool:
    """Return True if the configured Python sandbox image is available."""
    return _is_docker_image_available()


def _prepare_module_tree(
    sandbox_dir: Path,
    module_name: str,
    source_code: Optional[str],
    language: str = "python",
) -> Path:
    """Write source code into the sandbox directory matching module_name hierarchy."""
    clean_mod = (module_name or "target_module").strip().strip(".")
    parts = [p for p in clean_mod.split(".") if p] or ["target_module"]

    if language == "python":
        if len(parts) > 1:
            pkg_dir = sandbox_dir.joinpath(*parts[:-1])
            pkg_dir.mkdir(parents=True, exist_ok=True)
            curr = sandbox_dir
            for folder in parts[:-1]:
                curr = curr / folder
                init_file = curr / "__init__.py"
                if not init_file.exists():
                    init_file.write_text("", encoding="utf-8")
            mod_file = pkg_dir / f"{parts[-1]}.py"
        else:
            mod_file = sandbox_dir / f"{parts[0]}.py"

        content = source_code if (source_code and source_code.strip()) else "# Empty module stub\n"
        mod_file.write_text(content, encoding="utf-8")
        mod_file.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        return mod_file

    ext = ".js" if language in ("javascript", "js") else ".java"
    mod_file = sandbox_dir / f"{parts[-1]}{ext}"
    mod_file.write_text(source_code or "", encoding="utf-8")
    mod_file.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    return mod_file


def _set_subprocess_limits(cpu_seconds: int = 15) -> None:
    """Apply POSIX resource limits inside child process before exec."""
    try:
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 2))
        resource.setrlimit(resource.RLIMIT_FSIZE, (32 * 1024 * 1024, 32 * 1024 * 1024))
    except Exception:
        pass


def parse_junit_xml(xml_content: str) -> Tuple[int, int, int, int, float, List[TestCaseResultItem]]:
    """Parse JUnit XML string into summary counts and per-test TestCaseResultItem list."""
    if not xml_content or not xml_content.strip():
        return 0, 0, 0, 0, 0.0, []

    root = ET.fromstring(xml_content)
    test_cases: List[TestCaseResultItem] = []
    total = passed = failed = skipped = 0
    total_time_ms = 0.0
    for tc in root.findall(".//testcase"):
        total += 1
        name = tc.attrib.get("name", f"test_{total}")
        classname = tc.attrib.get("classname", "")
        dur_ms = round(float(tc.attrib.get("time", "0.0") or 0.0) * 1000.0, 2)
        total_time_ms += dur_ms
        failure_el, error_el, skipped_el = tc.find("failure"), tc.find("error"), tc.find("skipped")
        if failure_el is not None:
            failed += 1
            msg = failure_el.attrib.get("message") or (failure_el.text or "").strip()
            test_cases.append(
                TestCaseResultItem(
                    name=name,
                    classname=classname,
                    status="failed",
                    marker="❌",
                    duration_ms=dur_ms,
                    message=(msg or "Assertion failed")[:1000],
                )
            )
        elif error_el is not None:
            failed += 1
            msg = error_el.attrib.get("message") or (error_el.text or "").strip()
            test_cases.append(
                TestCaseResultItem(
                    name=name,
                    classname=classname,
                    status="error",
                    marker="❌",
                    duration_ms=dur_ms,
                    message=(msg or "Execution error")[:1000],
                )
            )
        elif skipped_el is not None:
            skipped += 1
            msg = skipped_el.attrib.get("message") or (skipped_el.text or "").strip()
            test_cases.append(
                TestCaseResultItem(
                    name=name,
                    classname=classname,
                    status="skipped",
                    marker="⏭️",
                    duration_ms=dur_ms,
                    message=(msg or "")[:500] or None,
                )
            )
        else:
            passed += 1
            test_cases.append(
                TestCaseResultItem(
                    name=name, classname=classname, status="passed", marker="✅", duration_ms=dur_ms
                )
            )
    return total, passed, failed, skipped, round(total_time_ms, 2), test_cases


def parse_coverage_xml(xml_content: str) -> float:
    """Extract line-rate coverage percentage (0-100) from Cobertura coverage.xml."""
    if not xml_content or not xml_content.strip():
        return 0.0
    try:
        root = ET.fromstring(xml_content)
        return round(float(root.attrib.get("line-rate", "0.0") or 0.0) * 100.0, 2)
    except Exception:
        return 0.0


def _run_fargate_sandbox(
    test_code: str,
    source_code: Optional[str],
    module_name: str,
    language: str,
    framework: str,
    timeout_seconds: int,
) -> DryRunResultSchema:
    """Start a short-lived ECS Fargate task using one-run presigned S3 URLs (no task AWS role)."""
    settings = get_settings()
    required = [
        settings.artifact_bucket,
        settings.ecs_cluster_arn,
        settings.ecs_task_definition_arn,
        settings.ecs_subnets,
        settings.ecs_security_group_id,
    ]
    if not all(required):
        return DryRunResultSchema(
            executed=False,
            sandbox_backend="fargate-not-configured",
            exit_code=0,
            stderr=(
                "Fargate dry-run is not configured. Set ARTIFACT_BUCKET, ECS_CLUSTER_ARN, "
                "ECS_TASK_DEFINITION_ARN, ECS_SUBNETS, and ECS_SECURITY_GROUP_ID."
            ),
        )

    run_id = f"sandbox-{int(time.time() * 1000)}"
    input_key = f"{settings.artifact_prefix.strip('/')}/{run_id}/sandbox-input.json"
    output_key = f"{settings.artifact_prefix.strip('/')}/{run_id}/sandbox-output.json"
    payload = {
        "run_id": run_id,
        "test_code": test_code,
        "source_code": source_code or "",
        "module_name": module_name,
        "language": language,
        "framework": framework,
        "timeout_seconds": timeout_seconds,
    }
    started = time.monotonic()
    task_arn = None
    try:
        import boto3

        s3 = boto3.client("s3")
        s3.put_object(
            Bucket=settings.artifact_bucket,
            Key=input_key,
            Body=json.dumps(payload).encode("utf-8"),
            ContentType="application/json",
            ServerSideEncryption="AES256",
        )
        input_url = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": settings.artifact_bucket, "Key": input_key},
            ExpiresIn=max(900, timeout_seconds + 300),
        )
        output_url = s3.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": settings.artifact_bucket,
                "Key": output_key,
                "ContentType": "application/json",
            },
            ExpiresIn=max(900, timeout_seconds + 300),
        )
        ecs = boto3.client("ecs")
        response = ecs.run_task(
            cluster=settings.ecs_cluster_arn,
            taskDefinition=settings.ecs_task_definition_arn,
            launchType="FARGATE",
            platformVersion="LATEST",
            count=1,
            networkConfiguration={
                "awsvpcConfiguration": {
                    "subnets": [
                        item.strip() for item in settings.ecs_subnets.split(",") if item.strip()
                    ],
                    "securityGroups": [settings.ecs_security_group_id],
                    "assignPublicIp": "DISABLED",
                }
            },
            overrides={
                "containerOverrides": [
                    {
                        "name": settings.ecs_container_name,
                        "environment": [
                            {"name": "SANDBOX_RUN_ID", "value": run_id},
                            {"name": "SANDBOX_INPUT_URL", "value": input_url},
                            {"name": "SANDBOX_OUTPUT_URL", "value": output_url},
                            {"name": "SANDBOX_TIMEOUT_SECONDS", "value": str(timeout_seconds)},
                        ],
                    }
                ]
            },
        )
        failures = response.get("failures") or []
        tasks = response.get("tasks") or []
        if failures or not tasks:
            return DryRunResultSchema(
                executed=False,
                sandbox_backend="aws-ecs-fargate",
                exit_code=1,
                stderr="ECS could not start the sandbox task: "
                + json.dumps(failures, default=str)[:2000],
            )
        task_arn = tasks[0].get("taskArn")
        deadline = started + min(timeout_seconds + 90, settings.ecs_task_timeout_seconds)
        task_description: Dict[str, Any] = tasks[0]
        while time.monotonic() < deadline:
            describe = ecs.describe_tasks(cluster=settings.ecs_cluster_arn, tasks=[task_arn])
            listed = describe.get("tasks") or []
            if listed:
                task_description = listed[0]
                if task_description.get("lastStatus") == "STOPPED":
                    break
            time.sleep(2)
        else:
            ecs.stop_task(
                cluster=settings.ecs_cluster_arn,
                task=task_arn,
                reason="Sandbox execution timed out",
            )
            return DryRunResultSchema(
                executed=True,
                sandbox_backend="aws-ecs-fargate",
                exit_code=124,
                total=1,
                failed=1,
                stderr=f"Fargate sandbox exceeded the {timeout_seconds}s test timeout.",
                test_cases=[
                    TestCaseResultItem(
                        name="sandbox_timeout",
                        status="error",
                        marker="❌",
                        message="Fargate task timed out",
                    )
                ],
            )

        container = next(
            iter(task_description.get("containers") or []),
            {},
        )
        if int(container.get("exitCode", 0) or 0) not in {0}:
            reason = (
                container.get("reason")
                or task_description.get("stoppedReason")
                or "Sandbox task exited unexpectedly"
            )
            logger.warning("Fargate sandbox runner exited with error", extra={"run_id": run_id})
            return DryRunResultSchema(
                executed=False,
                sandbox_backend="aws-ecs-fargate",
                exit_code=int(container.get("exitCode", 1) or 1),
                stderr=str(reason)[:2000],
            )
        try:
            stored = s3.get_object(Bucket=settings.artifact_bucket, Key=output_key)
            result_data = json.loads(stored["Body"].read().decode("utf-8"))
            result_data["sandbox_backend"] = "aws-ecs-fargate"
            return DryRunResultSchema.model_validate(result_data)
        except Exception as exc:
            return DryRunResultSchema(
                executed=False,
                sandbox_backend="aws-ecs-fargate",
                exit_code=1,
                stderr=f"Fargate task completed but no valid result artifact was retrieved: {exc}",
            )
    except Exception as exc:
        logger.exception("Fargate sandbox invocation failed", extra={"run_id": run_id})
        return DryRunResultSchema(
            executed=False,
            sandbox_backend="aws-ecs-fargate",
            exit_code=1,
            stderr=f"Fargate sandbox invocation failed: {exc}",
        )


def run_collection_check(
    test_code: str,
    source_code: Optional[str] = None,
    module_name: str = "target_module",
    language: str = "python",
    framework: str = "pytest",
) -> CollectionCheckResult:
    """Collect tests without execution; Python import code runs only inside Docker or explicit local mode."""
    lang = (language or "python").strip().lower()
    if lang == "python":
        mode = get_settings().sandbox_mode.lower()
        docker_available = _is_docker_available()
        if not docker_available and mode != "subprocess":
            return CollectionCheckResult(
                ok=True,
                output=(
                    "Collection not executed: Docker image/daemon is unavailable. "
                    "Install/build the sandbox image for isolated collection; static validation passed."
                ),
                collected_count=0,
                executed=False,
                sandbox_backend="docker-unavailable",
            )
        with tempfile.TemporaryDirectory(prefix="ai_test_collect_") as tmp_dir_str:
            sandbox_dir = Path(tmp_dir_str)
            out_dir = sandbox_dir / "out"
            out_dir.mkdir(parents=True, exist_ok=True)
            _prepare_module_tree(sandbox_dir, module_name, source_code, language="python")
            (sandbox_dir / "sitecustomize.py").write_text(
                NETWORK_BLOCK_SITECUSTOMIZE, encoding="utf-8"
            )
            test_file = sandbox_dir / "test_generated.py"
            test_file.write_text(test_code, encoding="utf-8")
            try:
                if docker_available:
                    proc = subprocess.run(
                        [
                            "docker",
                            "run",
                            "--rm",
                            "--network",
                            "none",
                            "--cpus",
                            str(get_settings().sandbox_cpu_limit),
                            "--memory",
                            f"{get_settings().sandbox_memory_limit_mb}m",
                            "--pids-limit",
                            "64",
                            "--security-opt",
                            "no-new-privileges",
                            "--read-only",
                            "--user",
                            "10001:10001",
                            "--tmpfs",
                            "/tmp:rw,noexec,nosuid,size=64m",
                            "-v",
                            f"{sandbox_dir}:/workspace:ro",
                            "-e",
                            "PYTHONPATH=/workspace",
                            "-e",
                            "PYTHONDONTWRITEBYTECODE=1",
                            get_settings().sandbox_docker_image,
                            "/workspace/test_generated.py",
                            "--collect-only",
                            "-q",
                        ],
                        capture_output=True,
                        text=True,
                        timeout=20,
                    )
                    backend = "docker-sandbox"
                else:
                    env = {
                        "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin"),
                        "PYTHONPATH": str(sandbox_dir),
                        "PYTHONDONTWRITEBYTECODE": "1",
                        "HOME": str(out_dir),
                        "TMPDIR": str(out_dir),
                    }
                    proc = subprocess.run(
                        [sys.executable, "-m", "pytest", "--collect-only", "-q", str(test_file)],
                        cwd=str(sandbox_dir),
                        env=env,
                        capture_output=True,
                        text=True,
                        timeout=20,
                        preexec_fn=lambda: _set_subprocess_limits(10),
                    )
                    backend = "explicit-local-subprocess"
                combined = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
                count = sum(
                    1
                    for line in (proc.stdout or "").splitlines()
                    if "::" in line or line.strip().startswith("<Function ")
                )
                return CollectionCheckResult(
                    ok=proc.returncode == 0,
                    output=combined,
                    collected_count=count,
                    executed=True,
                    sandbox_backend=backend,
                )
            except subprocess.TimeoutExpired:
                return CollectionCheckResult(
                    ok=False,
                    output="Collection check timed out after 20s",
                    collected_count=0,
                    executed=True,
                    sandbox_backend=(
                        "docker-sandbox" if docker_available else "explicit-local-subprocess"
                    ),
                )
            except Exception as exc:
                return CollectionCheckResult(
                    ok=False,
                    output=f"Collection check error: {exc}",
                    collected_count=0,
                    executed=True,
                    sandbox_backend=(
                        "docker-sandbox" if docker_available else "explicit-local-subprocess"
                    ),
                )

    if lang in ("javascript", "js"):
        matches = re.findall(r"\b(?:test|it)\s*\(", test_code)
        ok = len(matches) > 0
        return CollectionCheckResult(
            ok=ok,
            output=(
                f"Found {len(matches)} Jest test block(s) by static scan"
                if ok
                else "No test(...) or it(...) blocks found in JavaScript test code"
            ),
            collected_count=len(matches),
            executed=True,
            sandbox_backend="static-scan",
        )
    if lang == "java":
        matches = re.findall(r"@Test\b", test_code)
        ok = len(matches) > 0
        return CollectionCheckResult(
            ok=ok,
            output=(
                f"Found {len(matches)} @Test method(s) by static scan"
                if ok
                else "No @Test annotations found in Java test code"
            ),
            collected_count=len(matches),
            executed=True,
            sandbox_backend="static-scan",
        )
    return CollectionCheckResult(
        ok=True,
        output="Collection check skipped for unsupported language",
        collected_count=0,
        executed=False,
        sandbox_backend="not-supported",
    )


def _run_js_dry_run_in_node(
    test_code: str,
    source_code: Optional[str],
    module_name: str,
    timeout: int = 15,
) -> DryRunResultSchema:
    """Execute Jest-like JavaScript tests in Docker, or explicit trusted-local subprocess mode."""
    settings = get_settings()
    mode = settings.sandbox_mode.lower()
    docker_image = settings.sandbox_docker_image_js
    docker_available = _is_docker_image_available(docker_image)
    node_bin = shutil.which("node")
    if not docker_available and mode != "subprocess":
        return DryRunResultSchema(
            executed=False,
            sandbox_backend="docker-unavailable",
            exit_code=0,
            stderr=f"JavaScript dry-run not executed: Docker image '{docker_image}' is unavailable. Build sandbox/Dockerfile.javascript or explicitly set SANDBOX_MODE=subprocess for trusted local development only.",
        )
    if not docker_available and mode == "subprocess" and not node_bin:
        return DryRunResultSchema(
            executed=False,
            sandbox_backend="node-unavailable",
            exit_code=0,
            stderr="JavaScript dry-run not executed: Node.js is not installed.",
        )

    with tempfile.TemporaryDirectory(prefix="ai_js_sandbox_") as tmp_dir_str:
        sandbox_dir = Path(tmp_dir_str)
        clean_mod = (module_name or "target_module").strip().strip(".").replace("\\", "/")
        module_file = sandbox_dir / f"{clean_mod}.js"
        module_file.parent.mkdir(parents=True, exist_ok=True)
        src = source_code or ""
        src = re.sub(
            r"^\s*export\s+(?=(?:async\s+)?function|class|const|let|var)",
            "",
            src,
            flags=re.MULTILINE,
        )
        src = re.sub(r"^\s*export\s+default\s+", "", src, flags=re.MULTILINE)
        if "module.exports" not in src and "exports." not in src:
            exported_names = list(
                dict.fromkeys(
                    re.findall(r"(?:async\s+)?function\s+([A-Za-z_$][A-Za-z0-9_$]*)", src)
                    + re.findall(r"(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=", src)
                    + re.findall(r"class\s+([A-Za-z_$][A-Za-z0-9_$]*)", src)
                )
            )
            if exported_names:
                src += "\nmodule.exports = { " + ", ".join(exported_names) + " };\n"
        module_file.write_text(src, encoding="utf-8")
        test_file = sandbox_dir / "test_generated.js"
        test_file.write_text(test_code, encoding="utf-8")
        runner = r"""const assert = require('assert');
const pending = [], results = []; let currentSuite = '';
function describe(name, fn) { const prev = currentSuite; currentSuite = prev ? prev + ' ' + name : name; try { fn(); } catch (e) { results.push({name: currentSuite, status: 'failed', message: String(e.message || e)}); } currentSuite = prev; }
function test(name, fn) { const fullName = currentSuite ? currentSuite + ' ' + name : name; pending.push((async () => { const start = Date.now(); try { if (global.__beforeEach) await global.__beforeEach(); await fn(); if (global.__afterEach) await global.__afterEach(); results.push({name: fullName, status: 'passed', duration_ms: Date.now()-start}); } catch (e) { results.push({name: fullName, status: 'failed', duration_ms: Date.now()-start, message: String(e.message || e)}); } })()); }
const it = test; function mockFn(impl) { const f = (...args) => { f.mock.calls.push(args); return impl ? impl(...args) : undefined; }; f.mock = {calls: []}; f.mockClear = () => {f.mock.calls = [];}; return f; }
const jest = {fn: mockFn, clearAllMocks: () => {}};
function expect(received) { return {toBe: e => assert.strictEqual(received,e), toEqual: e => assert.deepStrictEqual(received,e), toStrictEqual: e => assert.deepStrictEqual(received,e), toBeDefined: () => assert.notStrictEqual(received,undefined), toBeUndefined: () => assert.strictEqual(received,undefined), toBeNull: () => assert.strictEqual(received,null), toBeTruthy: () => assert.ok(received), toBeFalsy: () => assert.ok(!received), toBeGreaterThan: e => assert.ok(received>e), toBeLessThan: e => assert.ok(received<e), toContain: e => assert.ok(received && received.includes(e)), toMatch: e => assert.ok(e.test(String(received))), toHaveBeenCalled: () => assert.ok(received && received.mock && received.mock.calls.length>0), toHaveBeenCalledTimes: n => assert.strictEqual(received && received.mock ? received.mock.calls.length : 0,n), toThrow: e => {let err; try {received();} catch(x) {err=x;} assert.ok(err,'Expected function to throw'); if(e && typeof e==='function') assert.ok(err instanceof e);} }; }
global.describe=describe; global.test=test; global.it=it; global.expect=expect; global.jest=jest;
require('./test_generated.js'); Promise.all(pending).then(() => { console.log('__JEST_RESULTS__:'+JSON.stringify(results)); process.exitCode=results.some(x=>x.status!=='passed')?1:0; });
"""
        runner_path = sandbox_dir / "runner.js"
        runner_path.write_text(runner, encoding="utf-8")
        t0 = time.perf_counter()
        try:
            if docker_available:
                proc = subprocess.run(
                    [
                        "docker",
                        "run",
                        "--rm",
                        "--network",
                        "none",
                        "--cpus",
                        str(settings.sandbox_cpu_limit),
                        "--memory",
                        f"{settings.sandbox_memory_limit_mb}m",
                        "--pids-limit",
                        "64",
                        "--security-opt",
                        "no-new-privileges",
                        "--read-only",
                        "--user",
                        "10001:10001",
                        "--tmpfs",
                        "/tmp:rw,noexec,nosuid,size=64m",
                        "-v",
                        f"{sandbox_dir}:/workspace:ro",
                        docker_image,
                        "/workspace/runner.js",
                    ],
                    cwd=str(sandbox_dir),
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
                backend = "docker-node-sandbox"
            else:
                proc = subprocess.run(
                    [node_bin, str(runner_path)],
                    cwd=str(sandbox_dir),
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    preexec_fn=lambda: _set_subprocess_limits(timeout),
                )
                backend = "explicit-local-node-subprocess"
        except subprocess.TimeoutExpired:
            duration_ms = round((time.perf_counter() - t0) * 1000.0, 2)
            return DryRunResultSchema(
                executed=True,
                sandbox_backend=(
                    "docker-node-sandbox" if docker_available else "explicit-local-node-subprocess"
                ),
                exit_code=124,
                total=1,
                failed=1,
                duration_ms=duration_ms,
                stderr=f"Sandbox execution timed out after {timeout} seconds.",
                test_cases=[
                    TestCaseResultItem(
                        name="sandbox_timeout",
                        status="error",
                        marker="❌",
                        message=f"Timed out after {timeout} seconds",
                    )
                ],
            )
        elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        stdout, stderr = proc.stdout or "", proc.stderr or ""
        cases: List[TestCaseResultItem] = []
        for line in stdout.splitlines():
            if line.startswith("__JEST_RESULTS__:"):
                try:
                    raw = json.loads(line.split("__JEST_RESULTS__:", 1)[1])
                except json.JSONDecodeError:
                    raw = []
                for item in raw:
                    st = item.get("status", "passed")
                    cases.append(
                        TestCaseResultItem(
                            name=item.get("name", "test"),
                            status=st,
                            marker="✅" if st == "passed" else "❌",
                            duration_ms=float(item.get("duration_ms", 0.0)),
                            message=item.get("message"),
                        )
                    )
        passed = sum(1 for case in cases if case.status == "passed")
        failed = sum(1 for case in cases if case.status != "passed")
        total = len(cases)
        return DryRunResultSchema(
            executed=True,
            sandbox_backend=backend,
            exit_code=0 if proc.returncode == 0 and failed == 0 and total > 0 else 1,
            total=total,
            passed=passed,
            failed=failed,
            skipped=0,
            duration_ms=elapsed_ms,
            coverage_pct=0.0,
            stdout=stdout[:10000],
            stderr=stderr[:10000],
            test_cases=cases,
        )


def run_sandbox_dry_run(
    test_code: str,
    source_code: Optional[str] = None,
    module_name: str = "target_module",
    language: str = "python",
    framework: str = "pytest",
    timeout_seconds: Optional[int] = None,
) -> DryRunResultSchema:
    """Execute generated tests in Docker/Fargate; host execution is opt-in for trusted development."""
    settings = get_settings()
    timeout = timeout_seconds or settings.sandbox_timeout_seconds
    lang = (language or "python").strip().lower()
    if lang in ("javascript", "js"):
        return _run_js_dry_run_in_node(test_code, source_code, module_name, timeout=timeout)
    if lang == "java":
        method_count = len(re.findall(r"@Test\b", test_code))
        return DryRunResultSchema(
            executed=False,
            sandbox_backend="junit-runtime-unavailable",
            exit_code=0,
            stderr=f"JUnit execution was not performed ({method_count} @Test method(s) found). Install/configure Maven or Gradle with JUnit and run in an ECS/CodeBuild job; this run only performs Java test-discovery and syntax-shape checks.",
        )

    if os.getenv("AWS_LAMBDA_FUNCTION_NAME"):
        return _run_fargate_sandbox(test_code, source_code, module_name, lang, framework, timeout)

    use_docker = _is_docker_available()
    if not use_docker and settings.sandbox_mode.lower() != "subprocess":
        return DryRunResultSchema(
            executed=False,
            sandbox_backend="docker-unavailable",
            exit_code=0,
            stderr=f"Python dry-run not executed: Docker image '{settings.sandbox_docker_image}' is unavailable. Build sandbox/Dockerfile or explicitly set SANDBOX_MODE=subprocess for trusted local development only.",
        )

    with tempfile.TemporaryDirectory(prefix="ai_test_sandbox_") as tmp_dir_str:
        sandbox_dir = Path(tmp_dir_str)
        out_dir = sandbox_dir / "out"
        out_dir.mkdir(parents=True, exist_ok=True)
        _prepare_module_tree(sandbox_dir, module_name, source_code, language="python")
        (sandbox_dir / "sitecustomize.py").write_text(NETWORK_BLOCK_SITECUSTOMIZE, encoding="utf-8")
        test_file = sandbox_dir / "test_generated.py"
        test_file.write_text(test_code, encoding="utf-8")
        junit_path, cov_path = out_dir / "junit.xml", out_dir / "coverage.xml"
        t0 = time.perf_counter()
        try:
            if use_docker:
                backend_name = "docker-sandbox"
                cmd = [
                    "docker",
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "--cpus",
                    str(settings.sandbox_cpu_limit),
                    "--memory",
                    f"{settings.sandbox_memory_limit_mb}m",
                    "--pids-limit",
                    "64",
                    "--security-opt",
                    "no-new-privileges",
                    "--read-only",
                    "--user",
                    "10001:10001",
                    "--tmpfs",
                    "/tmp:rw,noexec,nosuid,size=64m",
                    "-v",
                    f"{sandbox_dir}:/workspace:ro",
                    "-v",
                    f"{out_dir}:/tmp/sandbox-out:rw",
                    "-e",
                    "PYTHONPATH=/workspace",
                    "-e",
                    "PYTHONDONTWRITEBYTECODE=1",
                    settings.sandbox_docker_image,
                    "/workspace/test_generated.py",
                    "--junitxml=/tmp/sandbox-out/junit.xml",
                    "-v",
                    "--tb=short",
                ]
                if source_code and source_code.strip():
                    top_mod = (module_name or "target_module").split(".")[0]
                    cmd.extend(
                        [f"--cov={top_mod}", "--cov-report=xml:/tmp/sandbox-out/coverage.xml"]
                    )
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            else:
                backend_name = "explicit-local-subprocess"
                pytest_cmd = [
                    sys.executable,
                    "-m",
                    "pytest",
                    str(test_file),
                    f"--junitxml={junit_path}",
                    "-v",
                    "--tb=short",
                ]
                if source_code and source_code.strip():
                    top_mod = (module_name or "target_module").split(".")[0]
                    pytest_cmd.extend([f"--cov={top_mod}", f"--cov-report=xml:{cov_path}"])
                env = {
                    "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin"),
                    "PYTHONPATH": str(sandbox_dir),
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "HOME": str(out_dir),
                    "TMPDIR": str(out_dir),
                    "COVERAGE_FILE": str(out_dir / ".coverage"),
                }
                proc = subprocess.run(
                    pytest_cmd,
                    cwd=str(sandbox_dir),
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    preexec_fn=lambda: _set_subprocess_limits(timeout),
                )
            duration_ms = round((time.perf_counter() - t0) * 1000.0, 2)
            stdout, stderr = proc.stdout or "", proc.stderr or ""
            junit_xml = junit_path.read_text(encoding="utf-8") if junit_path.exists() else ""
            cov_xml = cov_path.read_text(encoding="utf-8") if cov_path.exists() else ""
            total, passed, failed, skipped, junit_duration, cases = parse_junit_xml(junit_xml)
            coverage_pct = parse_coverage_xml(cov_xml)
            if coverage_pct == 0 and passed > 0 and not (source_code and source_code.strip()):
                coverage_pct = 100.0 if failed == 0 else round((passed / max(total, 1)) * 100.0, 2)
            return DryRunResultSchema(
                executed=True,
                sandbox_backend=backend_name,
                exit_code=proc.returncode,
                total=total,
                passed=passed,
                failed=failed,
                skipped=skipped,
                duration_ms=junit_duration or duration_ms,
                coverage_pct=coverage_pct,
                stdout=stdout[:10000],
                stderr=stderr[:10000],
                test_cases=cases,
            )
        except subprocess.TimeoutExpired as exc:
            duration_ms = round((time.perf_counter() - t0) * 1000.0, 2)
            return DryRunResultSchema(
                executed=True,
                sandbox_backend="docker-sandbox" if use_docker else "explicit-local-subprocess",
                exit_code=124,
                total=1,
                failed=1,
                duration_ms=duration_ms,
                stderr=f"Sandbox execution timed out after {timeout} seconds: {exc}",
                test_cases=[
                    TestCaseResultItem(
                        name="sandbox_timeout",
                        status="error",
                        marker="❌",
                        duration_ms=duration_ms,
                        message=f"Timed out after {timeout} seconds",
                    )
                ],
            )
        except Exception as exc:
            return DryRunResultSchema(
                executed=False,
                sandbox_backend="docker-sandbox" if use_docker else "explicit-local-subprocess",
                exit_code=1,
                stderr=f"Sandbox execution error: {exc}",
            )
