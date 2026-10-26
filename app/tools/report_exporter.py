"""Report exporter for Markdown, PDF (fpdf2), and ZIP bundle downloads (Step 5)."""

from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime, timezone
from typing import Any, Dict, List

from fpdf import FPDF


def generate_markdown_report(run_data: Dict[str, Any]) -> str:
    """Build a comprehensive Markdown report summarizing requirement, spec, tests, and results."""
    run_id = run_data.get("run_id", "unknown")
    language = run_data.get("language", "python")
    framework = run_data.get("framework", "pytest")
    module_name = run_data.get("module_name", "target_module")
    requirement = run_data.get("requirement", "")
    final_result = run_data.get("final_result") or {}
    summary = final_result.get("summary") or {}
    badge = summary.get("badge", "✅ Passed")
    test_results: List[Dict[str, Any]] = final_result.get("test_results") or []
    parsed_spec: Dict[str, Any] = run_data.get("parsed_spec") or {}
    history: List[Dict[str, Any]] = run_data.get("history") or []
    test_code = run_data.get("test_code") or ""

    lines: List[str] = [
        "# AI-Powered Test Case Generation Report",
        "",
        f"- **Run ID:** `{run_id}`",
        f"- **Status Badge:** **{badge}**",
        f"- **Module:** `{module_name}`",
        f"- **Language / Framework:** `{language}` / `{framework}`",
        f"- **File Path:** `{run_data.get('file_path') or 'N/A'}`",
        f"- **Commit SHA:** `{run_data.get('commit_sha') or 'N/A'}`",
        f"- **Generated At:** `{run_data.get('created_at') or datetime.now(timezone.utc).isoformat()}`",
        "",
        "## 1. Requirement Specification",
        "",
        requirement,
        "",
        "## 2. Parsed Specification & Assumptions",
        "",
    ]

    funcs = parsed_spec.get("functions_under_test") or []
    if funcs:
        lines.append("### Functions Under Test")
        for fn in funcs:
            lines.append(f"- `{fn.get('signature', fn.get('name', ''))}` — {fn.get('purpose', '')}")
        lines.append("")

    edge_cases = parsed_spec.get("edge_cases") or []
    if edge_cases:
        lines.append("### Edge Cases Covered")
        for ec in edge_cases:
            lines.append(f"- {ec}")
        lines.append("")

    assumptions = parsed_spec.get("assumptions") or []
    if assumptions:
        lines.append("### Flagged Assumptions")
        for a in assumptions:
            lines.append(f"- {a}")
        lines.append("")

    lines.extend(
        [
            "## 3. Execution Summary",
            "",
            "| Metric | Value |",
            "|---|---|",
            f"| Final Status | {badge} |",
            f"| Total Tests | {summary.get('total', 0)} |",
            f"| Passed | {summary.get('passed', 0)} |",
            f"| Failed | {summary.get('failed', 0)} |",
            f"| Coverage | {summary.get('coverage_pct', 0.0)}% |",
            f"| Auto-Fixes Applied | {summary.get('auto_fixes', 0)} |",
            f"| Execution Duration | {summary.get('duration_ms', 0.0)} ms |",
            "",
            "## 4. Per-Test Results",
            "",
            "| Status | Test Case | Duration (ms) | Details |",
            "|---|---|---|---|",
        ]
    )

    for tc in test_results:
        marker = tc.get("marker", "✅")
        name = tc.get("name", "test")
        dur = tc.get("duration_ms", 0.0)
        msg = (tc.get("message") or "-").replace("\n", " ")[:120]
        lines.append(f"| {marker} `{tc.get('status', 'passed')}` | `{name}` | {dur} | {msg} |")

    lines.extend(
        [
            "",
            "## 5. Generated Test Code",
            "",
            f"```{language}",
            test_code.strip(),
            "```",
            "",
            "## 6. Agent Iteration History",
            "",
        ]
    )

    for item in history:
        lines.append(
            f"- **[{item.get('stage')}]** (Iteration {item.get('iteration', 0)}, "
            f"{item.get('duration_ms', 0.0)} ms): {item.get('output_summary', '')}"
        )
        if item.get("diff"):
            lines.extend(["", "```diff", item["diff"].strip(), "```", ""])

    return "\n".join(lines) + "\n"


def _ascii_clean(text: str) -> str:
    """Replace non-Latin-1 characters (like emoji badges) with safe ASCII equivalents for standard PDF fonts."""
    replacements = {
        "✅": "[PASS] ",
        "❌": "[FAIL] ",
        "🔧": "[FIXED] ",
        "⏭️": "[SKIP] ",
        "—": "-",
        "–": "-",
        "→": "->",
    }
    for k, v in replacements.items():
        text = text.replace(k, v)
    return text.encode("latin-1", errors="replace").decode("latin-1")


def generate_pdf_report(run_data: Dict[str, Any]) -> bytes:
    """Generate a PDF summary report using fpdf2."""
    md_text = generate_markdown_report(run_data)
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(
        0,
        10,
        _ascii_clean("AI-Powered Test Case Generator - Execution Report"),
        new_x="LMARGIN",
        new_y="NEXT",
    )
    pdf.ln(2)

    pdf.set_font("Courier", "", 9)
    for raw_line in md_text.splitlines():
        clean_line = _ascii_clean(raw_line)
        if not clean_line.strip():
            pdf.ln(3)
            continue
        # Wrap long lines safely
        while len(clean_line) > 95:
            chunk, clean_line = clean_line[:95], clean_line[95:]
            pdf.cell(0, 4.5, chunk, new_x="LMARGIN", new_y="NEXT")
        pdf.cell(0, 4.5, clean_line, new_x="LMARGIN", new_y="NEXT")

    return bytes(pdf.output())


def generate_zip_bundle(run_data: Dict[str, Any]) -> bytes:
    """Create a downloadable ZIP archive containing test file, source code, Markdown/PDF report, and JSON metadata."""
    buf = io.BytesIO()
    lang = (run_data.get("language") or "python").lower()
    ext = ".py" if lang == "python" else (".test.js" if lang == "javascript" else ".java")
    mod = run_data.get("module_name") or "target_module"

    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"test_{mod}{ext}", run_data.get("test_code") or "")
        if run_data.get("source_code"):
            src_ext = ".py" if lang == "python" else (".js" if lang == "javascript" else ".java")
            zf.writestr(f"{mod}{src_ext}", run_data["source_code"])
        zf.writestr("REPORT.md", generate_markdown_report(run_data))
        zf.writestr("REPORT.pdf", generate_pdf_report(run_data))
        zf.writestr("run_metadata.json", json.dumps(run_data, indent=2, default=str))

    return buf.getvalue()
