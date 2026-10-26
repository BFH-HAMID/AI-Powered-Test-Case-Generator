"""Streamlit UI for requirements, source upload, live progress, editable tests, and results delivery."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

import httpx
import streamlit as st
from dotenv import load_dotenv

try:
    from streamlit_ace import st_ace
except Exception:  # pragma: no cover - optional editor compatibility
    st_ace = None

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
BACKEND_URL = os.getenv("BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")
API_KEY = os.getenv("API_KEY", "")

LANGUAGE_FRAMEWORKS = {
    "Python": ["pytest", "unittest"],
    "JavaScript": ["jest"],
    "Java": ["junit"],
}
LANGUAGE_IDS = {"Python": "python", "JavaScript": "javascript", "Java": "java"}
FRAMEWORK_IDS = {"pytest": "pytest", "unittest": "unittest", "jest": "jest", "junit": "junit"}

st.set_page_config(
    page_title="AI Test Generator",
    page_icon="🧪",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
<style>
  .stApp { background: #f6f8fc; }
  .block-container { padding-top: 1.5rem; max-width: 1440px; }
  .hero { padding: 1.5rem 1.8rem; border-radius: 18px; color: white;
    background: linear-gradient(115deg, #111827 0%, #243b67 52%, #5b4ce2 100%);
    margin-bottom: 1.25rem; box-shadow: 0 16px 38px rgba(35, 49, 93, .18); }
  .hero h1 { font-size: 2.25rem; margin: 0 0 .35rem 0; letter-spacing: -.04em; }
  .hero p { color: #dbe7ff; margin: 0; font-size: 1rem; }
  .section-title { font-weight: 700; color: #17243a; font-size: 1.05rem; }
  div[data-testid="stMetric"] { background: white; border: 1px solid #e7ecf4; padding: 12px 16px; border-radius: 14px; }
  .small-note { color: #64748b; font-size: .85rem; }
</style>
<div class="hero">
  <h1>🧪 AI-Powered Test Case Generator</h1>
  <p>Turn requirements and source code into reviewed, validated, and downloadable tests.</p>
</div>
""",
    unsafe_allow_html=True,
)


def _headers() -> Dict[str, str]:
    headers = {"X-Request-ID": f"ui-{int(time.time() * 1000)}"}
    if API_KEY:
        headers["X-API-Key"] = API_KEY
    return headers


def _api_client(timeout: float = 20.0) -> httpx.Client:
    return httpx.Client(base_url=BACKEND_URL, headers=_headers(), timeout=timeout)


def _request_json(method: str, path: str, **kwargs: Any) -> Dict[str, Any]:
    with _api_client(timeout=kwargs.pop("timeout", 20.0)) as client:
        response = client.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json()


def _stage_label(stage: str) -> str:
    labels = {
        "queued": "Queued",
        "parsing": "Parsing requirement",
        "generating": "Generating tests",
        "validating": "Validating & sandboxing",
        "fixing": "Repairing failed tests",
        "saving": "Saving test file",
        "ci_running": "Checking GitHub Actions",
        "collecting": "Collecting results",
        "completed": "Complete",
        "failed": "Failed",
        "failed_validation": "Validation failed",
    }
    return labels.get(stage, stage.replace("_", " ").title())


def _run_generation(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        created = _request_json("POST", "/generate", json=payload)
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text[:1000]
        st.error(f"API rejected the generation request ({exc.response.status_code}): {detail}")
        return None
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        st.error(f"Could not reach the API at {BACKEND_URL}: {exc}")
        return None
    except Exception as exc:  # pragma: no cover - defensive UI error handling
        st.error(f"Unexpected API error: {exc}")
        return None

    run_id = created.get("run_id")
    if not run_id:
        st.error("The API accepted the request but did not return a run ID.")
        return None
    st.session_state["active_run_id"] = run_id
    st.session_state["active_stage"] = "queued"
    st.session_state["active_result"] = None

    progress_slot = st.empty()
    stage_slot = st.empty()
    status_slot = st.status("Generation started", expanded=True)
    deadline = time.monotonic() + 15 * 60
    last_status = None
    while time.monotonic() < deadline:
        try:
            status_data = _request_json("GET", f"/status/{run_id}", timeout=12.0)
        except Exception as exc:
            status_slot.update(label="Waiting for API status", state="running")
            stage_slot.warning(f"Status polling failed temporarily: {exc}")
            time.sleep(2)
            continue

        progress = int(status_data.get("progress", 0) or 0)
        stage = str(status_data.get("stage", "queued"))
        status = str(status_data.get("status", "pending"))
        st.session_state["active_stage"] = stage
        progress_slot.progress(max(0, min(progress, 100)), text=f"{progress}%")
        stage_slot.info(f"**{_stage_label(stage)}** · {status.replace('_', ' ').title()}")
        if last_status != (stage, status):
            status_slot.write(f"{_stage_label(stage)} — {progress}%")
            last_status = (stage, status)

        if status in {"completed", "failed", "failed_validation"} or stage in {
            "completed",
            "failed",
        }:
            break
        time.sleep(1.5)
    else:
        status_slot.update(label="Generation is still running", state="running")
        st.warning(
            f"Polling timed out. Your run is still available as `{run_id}`; reload it from history."
        )
        return None

    try:
        result = _request_json("GET", f"/result/{run_id}", timeout=30.0)
    except Exception as exc:
        st.error(f"The run finished, but results could not be loaded: {exc}")
        return None
    st.session_state["active_result"] = result
    final_status = result.get("summary", {}).get("badge") or result.get("status", "completed")
    status_slot.update(
        label=f"Generation finished · {final_status}",
        state="complete" if result.get("status") == "completed" else "error",
    )
    return result


def _load_run(run_id: str) -> Optional[Dict[str, Any]]:
    try:
        result = _request_json("GET", f"/result/{run_id}", timeout=30.0)
        st.session_state["active_run_id"] = run_id
        st.session_state["active_result"] = result
        return result
    except Exception as exc:
        st.error(f"Could not load run `{run_id}`: {exc}")
        return None


def _render_results(result: Dict[str, Any]) -> None:
    run_id = result.get("run_id", "")
    summary = result.get("summary") or {}
    status = summary.get("badge", "Results")
    st.subheader(f"Run `{run_id}` · {status}")

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Total", summary.get("total", 0))
    c2.metric("Passed", summary.get("passed", 0))
    c3.metric("Failed", summary.get("failed", 0))
    c4.metric("Coverage", f"{summary.get('coverage_pct', 0.0)}%")
    c5.metric("Auto-fixes", summary.get("auto_fixes", 0))

    if result.get("validation_report"):
        report = result["validation_report"]
        warnings = report.get("warnings") or []
        for warning in warnings:
            st.warning(warning)
        dry = report.get("dry_run_results") or {}
        if not dry.get("executed") and not warnings:
            st.info("Static validation completed; no sandbox runtime result is available.")

    code_tab, tests_tab, logs_tab, history_tab = st.tabs(
        ["Generated test code", "Test results", "Logs", "Iteration history"]
    )
    with code_tab:
        language = result.get("syntax_highlighting", result.get("language", "python"))
        code = result.get("test_code", "") or ""
        editor_key = f"test-editor-{run_id}"
        if st_ace is not None:
            edited = st_ace(
                value=code,
                language=language,
                theme="tomorrow_night",
                key=editor_key,
                height=420,
                auto_update=True,
                wrap=True,
                show_gutter=True,
                font_size=14,
            )
        else:
            edited = st.text_area("Edit generated code", code, height=420, key=editor_key)
        extension = {"python": "py", "javascript": "js", "java": "java"}.get(language, "txt")
        left, right, third = st.columns([1, 1, 1])
        left.download_button(
            "⬇ Download test file",
            data=edited or code,
            file_name=f"generated_tests.{extension}",
            mime="text/plain",
            key=f"download-code-{run_id}",
        )
        if right.button("↻ Re-validate", key=f"revalidate-{run_id}", use_container_width=True):
            try:
                response = _request_json(
                    "POST",
                    f"/revalidate/{run_id}",
                    json={"test_code": edited or code, "run_sandbox_dry_run": True},
                    timeout=90.0,
                )
                st.success(
                    "Re-validation finished."
                    if response.get("validation_report", {}).get("valid")
                    else "Validation found issues."
                )
                _load_run(run_id)
                st.rerun()
            except Exception as exc:
                st.error(f"Could not re-validate edited code: {exc}")
        if third.button("Commit to repo", key=f"commit-{run_id}", use_container_width=True):
            try:
                commit = _request_json(
                    "POST",
                    f"/commit/{run_id}",
                    json={"test_code": edited or code, "create_pr": create_pr},
                    timeout=45.0,
                )
                st.success(
                    f"Committed `{commit.get('relative_path')}` · {commit.get('commit_sha') or 'local save'}"
                )
                _load_run(run_id)
                st.rerun()
            except httpx.HTTPStatusError as exc:
                st.error(f"Commit rejected: {exc.response.text[:1000]}")
            except Exception as exc:
                st.error(f"Could not commit tests: {exc}")

        download_cols = st.columns(3)
        for col, label, suffix, mime in [
            (download_cols[0], "Markdown report", "report.md", "text/markdown"),
            (download_cols[1], "PDF report", "report.pdf", "application/pdf"),
            (download_cols[2], "ZIP bundle", "bundle.zip", "application/zip"),
        ]:
            try:
                with _api_client(timeout=30.0) as client:
                    response = client.get(f"/download/{run_id}/{suffix}")
                    if response.is_success:
                        col.download_button(
                            f"⬇ {label}",
                            data=response.content,
                            file_name=f"test-generator-{run_id}.{suffix.split('.')[-1]}",
                            mime=mime,
                            key=f"download-{suffix}-{run_id}",
                        )
            except Exception:
                col.caption(f"{label} unavailable")

    with tests_tab:
        cases = result.get("test_results") or []
        if cases:
            for case in cases:
                marker = case.get("marker") or ("✅" if case.get("status") == "passed" else "❌")
                duration = case.get("duration_ms", 0.0)
                title = f"{marker} `{case.get('name', 'test')}` · {case.get('status', 'unknown')} · {duration} ms"
                if case.get("message"):
                    with st.expander(title):
                        st.code(case["message"])
                else:
                    st.write(title)
        else:
            report = result.get("validation_report") or {}
            dry = report.get("dry_run_results") or {}
            st.info(
                "No per-test execution results are available. "
                + (dry.get("stderr") or "The test suite may have passed static validation only.")
            )
        ci = result.get("ci_result") or {}
        if ci:
            st.caption(
                f"CI provider: {ci.get('provider', 'local')} · {ci.get('status', 'unknown')} · {ci.get('conclusion', 'n/a')}"
            )
            if ci.get("html_url"):
                st.link_button("Open GitHub Actions run", ci["html_url"])
        if result.get("file_path"):
            st.caption(f"Saved file: `{result['file_path']}`")
        if result.get("commit_sha"):
            st.caption(f"Commit: `{result['commit_sha']}`")
        if result.get("pr_url"):
            st.link_button("Open pull request", result["pr_url"])
        if result.get("pr_comment_url"):
            st.link_button("Open PR summary comment", result["pr_comment_url"])

        with st.form(f"feedback-form-{run_id}"):
            st.markdown("**Rate these tests**")
            rating = st.slider("Rating", 1, 5, 4, key=f"rating-{run_id}")
            feedback_comment = st.text_input("Optional feedback", key=f"feedback-comment-{run_id}")
            submit_feedback = st.form_submit_button("Send feedback")
        if submit_feedback:
            try:
                _request_json(
                    "POST",
                    f"/feedback/{run_id}",
                    json={"rating": rating, "comment": feedback_comment, "helpful_tags": []},
                )
                st.success("Thanks — feedback recorded.")
            except Exception as exc:
                st.error(f"Could not record feedback: {exc}")

    with logs_tab:
        logs = result.get("error_logs") or []
        if logs:
            for item in logs:
                st.error(item)
        else:
            st.success("No correction or runtime errors were recorded.")
        report = result.get("validation_report") or {}
        with st.expander("Validation report JSON"):
            st.json(report)

    with history_tab:
        history = result.get("history") or []
        if not history:
            st.info("No iteration history is available yet.")
        for item in history:
            label = (
                f"{item.get('stage', 'stage').title()} · iteration {item.get('iteration', 0)} · "
                f"{item.get('duration_ms', 0.0)} ms · {item.get('status', 'ok')}"
            )
            with st.expander(label):
                st.write(item.get("input_summary", ""))
                st.write(item.get("output_summary", ""))
                if item.get("errors"):
                    for error in item["errors"]:
                        st.error(error)
                if item.get("diff"):
                    st.code(item["diff"], language="diff")


with st.sidebar:
    st.header("⚙️ Run settings")
    model = st.selectbox(
        "Model",
        ["Configured provider", "gpt-4o", "gpt-4o-mini", "qwen2.5-coder:7b", "local-synthesizer"],
        help="Provider/API keys are configured on the backend in .env.",
    )
    max_retries = st.slider("Maximum repair attempts", 0, 8, 3)
    coverage_target = st.slider("Coverage target (%)", 0, 100, 85, step=5)
    include_edge_cases = st.toggle("Include edge cases", value=True)
    include_negative_tests = st.toggle("Include negative tests", value=True)
    run_sandbox = st.toggle("Run in configured sandbox", value=True)
    auto_commit = st.toggle("Save tests and create a Git branch/commit", value=True)
    create_pr = st.toggle("Open a pull request (requires GitHub token + repo)", value=False)
    trigger_ci = st.toggle("Collect GitHub Actions result", value=True)
    st.divider()
    st.caption(f"Backend: `{BACKEND_URL}`")
    st.caption(
        "Generated code is not run on this Streamlit server. Docker is required for isolated dry-runs."
    )

col_left, col_right = st.columns([1.5, 1])
with col_left:
    st.markdown(
        '<div class="section-title">1 · Describe what should be tested</div>',
        unsafe_allow_html=True,
    )
    requirement = st.text_area(
        "Requirement / user story",
        placeholder="Example: add(a, b) returns the sum of two numbers and rejects non-numeric input.",
        height=140,
        key="requirement-input",
        label_visibility="collapsed",
    )
with col_right:
    st.markdown(
        '<div class="section-title">2 · Configure the implementation</div>', unsafe_allow_html=True
    )
    uploaded = st.file_uploader("Upload source file", type=["py", "js", "jsx", "ts", "tsx", "java"])
    if uploaded is not None:
        source_file_name = uploaded.name
        try:
            uploaded_code = uploaded.getvalue().decode("utf-8")
        except UnicodeDecodeError:
            uploaded_code = uploaded.getvalue().decode("utf-8", errors="replace")
    else:
        source_file_name = ""
        uploaded_code = ""

language_label = st.selectbox("Language", list(LANGUAGE_FRAMEWORKS), index=0)
framework_label = st.selectbox("Test framework", LANGUAGE_FRAMEWORKS[language_label])
default_module = Path(source_file_name).stem if source_file_name else "target_module"
module_name = st.text_input(
    "Module / import path",
    value=default_module,
    help="Python: package.module; JavaScript: module path; Java: class/package hint",
)
source_code = st.text_area(
    "Paste source code (optional; overrides uploaded source when non-empty)",
    value=uploaded_code,
    height=280,
    key="source-code-input",
)
repo_col, branch_col = st.columns(2)
with repo_col:
    repo_url = st.text_input(
        "GitHub repo URL (optional)",
        placeholder="owner/repository or https://github.com/owner/repository",
    )
with branch_col:
    branch = st.text_input("Base branch", value="main")

button_col, help_col = st.columns([1, 2])
with button_col:
    generate_clicked = st.button("✨ Generate tests", type="primary", use_container_width=True)
with help_col:
    st.markdown(
        '<p class="small-note">Runs parse → generate → validation → save → CI collection. You can edit, re-validate, or commit the output below.</p>',
        unsafe_allow_html=True,
    )

if generate_clicked:
    if len(requirement.strip()) < 3:
        st.error("Enter a requirement or user story with at least three characters.")
    else:
        payload = {
            "requirement": requirement.strip(),
            "source_code": source_code or None,
            "module_name": module_name.strip() or "target_module",
            "language": LANGUAGE_IDS[language_label],
            "framework": FRAMEWORK_IDS[framework_label],
            "repo": repo_url.strip() or None,
            "branch": branch.strip() or "main",
            "options": {
                "model": model if model != "Configured provider" else "configured",
                "max_retries": max_retries,
                "coverage_target": coverage_target,
                "include_edge_cases": include_edge_cases,
                "include_negative_tests": include_negative_tests,
                "run_sandbox_dry_run": run_sandbox,
                "auto_commit": auto_commit,
                "create_pr": create_pr,
                "trigger_ci": trigger_ci,
                "sync": False,
            },
        }
        _run_generation(payload)

with st.expander("📚 Previous runs", expanded=False):
    try:
        history = _request_json("GET", "/history?limit=30")
        items = history.get("items", [])
        if items:
            labels = {
                f"{item.get('created_at', '')[:16]} · {item.get('status', '')} · {item.get('requirement_preview', '')}": item[
                    "run_id"
                ]
                for item in items
            }
            selected = st.selectbox("Choose a run", ["Select a run"] + list(labels))
            if selected != "Select a run" and st.button("Load selected run"):
                _load_run(labels[selected])
                st.rerun()
        else:
            st.caption("No previous runs yet.")
    except Exception as exc:
        st.caption(f"History is unavailable: {exc}")

active_result = st.session_state.get("active_result")
if active_result:
    st.divider()
    _render_results(active_result)
elif st.session_state.get("active_run_id"):
    st.info(
        f"Current run: `{st.session_state['active_run_id']}` · {_stage_label(st.session_state.get('active_stage', 'queued'))}"
    )
