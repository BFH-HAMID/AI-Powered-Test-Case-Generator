"""Pydantic v2 request and response schemas for the FastAPI gateway."""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_REQUIREMENT_CHARS = 20_000
MAX_SOURCE_CODE_CHARS = 100_000


class FunctionUnderTest(BaseModel):
    """Describes a target function or method extracted from requirement/source."""

    model_config = ConfigDict(extra="ignore")

    name: str = Field(..., description="Function or method name")
    signature: str = Field(..., description="Full function signature with parameter types")
    purpose: str = Field(..., description="Concise summary of the function's purpose")


class ExpectedBehaviorItem(BaseModel):
    """Input -> expected output specification."""

    model_config = ConfigDict(extra="ignore")

    input: str = Field(..., description="Sample or symbolic input arguments")
    expected_output: str = Field(..., description="Expected return value or state change")


class NegativeTestItem(BaseModel):
    """Invalid scenario -> expected error/exception specification."""

    model_config = ConfigDict(extra="ignore")

    scenario: str = Field(..., description="Description of invalid input or failure condition")
    expected_error: str = Field(..., description="Expected exception class or error behavior")


class ParsedRequirementSpec(BaseModel):
    """Structured specification produced by the requirement parsing node (Step 4a)."""

    model_config = ConfigDict(extra="ignore")

    functions_under_test: List[FunctionUnderTest] = Field(default_factory=list)
    core_functionality: List[str] = Field(default_factory=list)
    expected_behavior: List[ExpectedBehaviorItem] = Field(default_factory=list)
    edge_cases: List[str] = Field(default_factory=list)
    negative_tests: List[NegativeTestItem] = Field(default_factory=list)
    dependencies_to_mock: List[str] = Field(default_factory=list)
    setup_teardown_needs: List[str] = Field(default_factory=list)
    assumptions: List[str] = Field(default_factory=list)


class TestCaseResultItem(BaseModel):
    """Execution result for an individual test case."""

    model_config = ConfigDict(extra="ignore")

    name: str
    status: Literal["passed", "failed", "skipped", "error"] = "passed"
    marker: str = "✅"
    duration_ms: float = 0.0
    message: Optional[str] = None
    classname: Optional[str] = None


class DryRunResultSchema(BaseModel):
    """Output captured from the sandboxed test execution."""

    model_config = ConfigDict(extra="ignore")

    executed: bool = False
    sandbox_backend: str = "subprocess-isolated"
    exit_code: int = 0
    total: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    duration_ms: float = 0.0
    coverage_pct: float = 0.0
    stdout: str = ""
    stderr: str = ""
    test_cases: List[TestCaseResultItem] = Field(default_factory=list)


class ValidationReportSchema(BaseModel):
    """Structured validation report produced by the validation pipeline (Step 4c)."""

    model_config = ConfigDict(extra="ignore")

    valid: bool = False
    syntax_ok: bool = False
    security_ok: bool = True
    security_issues: List[str] = Field(default_factory=list)
    lint_ok: bool = True
    lint_issues: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    collection_executed: bool = False
    collect_ok: bool = False
    collect_output: str = ""
    dry_run_ok: bool = False
    dry_run_results: DryRunResultSchema = Field(default_factory=DryRunResultSchema)
    errors: List[str] = Field(default_factory=list)
    coverage: float = 0.0
    formatted_code: Optional[str] = None


class GenerateOptions(BaseModel):
    """User-configurable options for test generation."""

    model_config = ConfigDict(extra="ignore")

    model: str = Field(
        default="configured", description="LLM model identifier or configured provider"
    )
    max_retries: int = Field(default=3, ge=0, le=10, description="Max fix loop retries")
    coverage_target: int = Field(default=85, ge=0, le=100, description="Target coverage %")
    include_edge_cases: bool = Field(default=True, description="Generate boundary/edge tests")
    include_negative_tests: bool = Field(default=True, description="Generate exception tests")
    run_sandbox_dry_run: bool = Field(default=True, description="Run tests in sandbox")
    auto_commit: bool = Field(default=True, description="Save file and commit to Git branch")
    create_pr: bool = Field(default=False, description="Open a GitHub Pull Request")
    trigger_ci: bool = Field(default=True, description="Run/collect CI results")
    sync: bool = Field(
        default=False,
        description="If True, wait for completion synchronously before returning",
    )


class GenerateRequest(BaseModel):
    """POST /generate request payload."""

    model_config = ConfigDict(extra="ignore")

    requirement: str = Field(
        ...,
        min_length=3,
        max_length=MAX_REQUIREMENT_CHARS,
        description="Natural language requirement, user story, or specification",
    )
    source_code: Optional[str] = Field(
        default=None,
        max_length=MAX_SOURCE_CODE_CHARS,
        description="Optional source code under test",
    )
    module_name: str = Field(
        default="target_module",
        min_length=1,
        max_length=120,
        pattern=r"^[a-zA-Z_][a-zA-Z0-9_.]*$",
        description="Module name for test imports",
    )
    language: Literal["python", "javascript", "java"] = Field(
        default="python", description="Programming language"
    )
    framework: Literal["pytest", "unittest", "jest", "junit"] = Field(
        default="pytest", description="Test framework"
    )
    repo: Optional[str] = Field(
        default=None, max_length=512, description="Optional GitHub repo URL or owner/repo"
    )
    branch: Optional[str] = Field(default="main", max_length=128, description="Target base branch")
    options: GenerateOptions = Field(default_factory=GenerateOptions)

    @field_validator("language", mode="before")
    @classmethod
    def normalize_language(cls, v: Any) -> str:
        if isinstance(v, str):
            return v.strip().lower()
        return "python"

    @field_validator("framework", mode="before")
    @classmethod
    def normalize_framework(cls, v: Any) -> str:
        if isinstance(v, str):
            return v.strip().lower()
        return "pytest"

    @model_validator(mode="after")
    def validate_language_framework_pair(self) -> "GenerateRequest":
        allowed = {
            "python": {"pytest", "unittest"},
            "javascript": {"jest"},
            "java": {"junit"},
        }
        if self.framework not in allowed[self.language]:
            choices = ", ".join(sorted(allowed[self.language]))
            raise ValueError(
                f"Framework '{self.framework}' is not supported for {self.language}; use {choices}."
            )
        return self


class GenerateResponse(BaseModel):
    """Response returned immediately when a generation job is created."""

    run_id: str
    status: str
    current_stage: str
    message: str


class RevalidateRequest(BaseModel):
    """POST /revalidate/{run_id} request payload when user edits test code in UI."""

    test_code: str = Field(
        ...,
        min_length=1,
        max_length=MAX_SOURCE_CODE_CHARS,
        description="Edited test code to re-validate and execute in sandbox",
    )
    run_sandbox_dry_run: bool = Field(default=True)


class CommitRequest(BaseModel):
    """POST /commit/{run_id} request payload to save and commit test code."""

    test_code: Optional[str] = Field(
        default=None,
        max_length=MAX_SOURCE_CODE_CHARS,
        description="Optional edited test code override before committing",
    )
    commit_message: Optional[str] = Field(
        default=None,
        max_length=280,
        description="Custom Git commit message",
    )
    create_pr: bool = Field(default=False)


class PRCommentRequest(BaseModel):
    """Request to post the completed run summary to a GitHub Pull Request."""

    pr_number: int = Field(..., gt=0)


class FeedbackRequest(BaseModel):
    """POST /feedback/{run_id} request payload for rating generated tests."""

    rating: int = Field(..., ge=1, le=5, description="Star rating from 1 to 5")
    comment: Optional[str] = Field(default=None, max_length=2000)
    helpful_tags: List[str] = Field(default_factory=list)


class StatusResponse(BaseModel):
    """GET /status/{run_id} response for live UI progress polling."""

    run_id: str
    status: str
    stage: str
    progress: int
    iteration: int
    max_iterations: int
    partial_outputs: Dict[str, Any] = Field(default_factory=dict)
    history: List[Dict[str, Any]] = Field(default_factory=list)
    error_message: Optional[str] = None


class ResultSummarySchema(BaseModel):
    """Aggregated summary metrics for a completed run."""

    total: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    coverage_pct: float = 0.0
    auto_fixes: int = 0
    duration_ms: float = 0.0
    final_status: Literal["Passed", "Fixed", "Failed", "Validated only", "Not Run"] = "Passed"
    badge: str = "✅ Passed"


class ResultResponse(BaseModel):
    """GET /result/{run_id} response with UI-ready output delivery fields."""

    run_id: str
    status: str
    stage: str
    language: str
    framework: str
    syntax_highlighting: str
    module_name: str
    requirement: str
    source_code: Optional[str] = None
    parsed_spec: Optional[Dict[str, Any]] = None
    test_code: str = ""
    summary: ResultSummarySchema = Field(default_factory=ResultSummarySchema)
    test_results: List[TestCaseResultItem] = Field(default_factory=list)
    validation_report: Optional[Dict[str, Any]] = None
    ci_result: Optional[Dict[str, Any]] = None
    error_logs: List[str] = Field(default_factory=list)
    history: List[Dict[str, Any]] = Field(default_factory=list)
    file_path: Optional[str] = None
    commit_sha: Optional[str] = None
    pr_url: Optional[str] = None
    pr_comment_url: Optional[str] = None
    feedbacks: List[Dict[str, Any]] = Field(default_factory=list)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
