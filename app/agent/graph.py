"""LangGraph workflow for requirement parsing, test generation, validation/retry, saving, CI, and results."""

from __future__ import annotations

import asyncio
import time
from typing import Any, AsyncIterator, Callable, Dict

from langgraph.graph import END, START, StateGraph
from langgraph.types import TimeoutPolicy

from app.agent.nodes.collect_results import collect_results_node
from app.agent.nodes.fix_tests import fix_tests_node
from app.agent.nodes.generate_tests import generate_tests_node
from app.agent.nodes.parse_requirement import parse_requirement_node
from app.agent.nodes.run_ci import run_ci_node
from app.agent.nodes.save_files import save_files_node
from app.agent.nodes.validate import validate_tests_node
from app.agent.state import AgentState
from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.session import append_iteration_record, update_run_stage

logger = get_logger(__name__)


def _safe_node(name: str, fn: Callable[[Dict[str, Any]], Dict[str, Any]]):
    """Wrap a graph node with visible exception handling and failed-run persistence.

    Remote LLM requests and subprocess tools carry their own timeouts. This wrapper ensures
    unexpected errors are captured in state/history instead of escaping silently.
    """

    async def wrapped(state: Dict[str, Any]) -> Dict[str, Any]:
        started = time.perf_counter()
        try:
            # Isolate blocking LangChain/SQLAlchemy/tool calls from the event loop. LangGraph
            # applies an async run_timeout; individual HTTP/subprocess calls have stricter timeouts.
            return await asyncio.to_thread(fn, state)
        except Exception as exc:
            error = f"{name} node failed: {type(exc).__name__}: {exc}"
            run_id = str(state.get("run_id", ""))
            logger.exception("Agent graph node failed", extra={"run_id": run_id, "stage": name})
            history = list(state.get("history") or [])
            event = {
                "iteration": int(state.get("iteration", 0)),
                "stage": name,
                "status": "failed",
                "input_summary": f"run={run_id}",
                "output_summary": error,
                "duration_ms": round((time.perf_counter() - started) * 1000.0, 2),
                "errors": [error],
            }
            history.append(event)
            errors = list(state.get("errors") or []) + [error]
            failed_state = {
                **state,
                "history": history,
                "errors": errors,
                "status": "failed",
                "current_stage": name,
                "progress": max(0, int(state.get("progress", 0))),
                "graph_error": error,
            }
            try:
                append_iteration_record(
                    run_id=run_id,
                    iteration_number=int(state.get("iteration", 0)),
                    stage=name,
                    status="failed",
                    input_summary=f"run={run_id}",
                    output_summary=error,
                    duration_ms=event["duration_ms"],
                    test_code_snapshot=state.get("test_code"),
                    errors=[error],
                )
                update_run_stage(
                    run_id=run_id,
                    stage=name,
                    progress=failed_state["progress"],
                    status="failed",
                    error_message=error,
                )
            except Exception:
                logger.exception("Could not persist graph failure", extra={"run_id": run_id})
            return failed_state

    wrapped.__name__ = f"safe_{name}"
    return wrapped


def _route_parse(state: AgentState) -> str:
    return "collect_results" if state.get("graph_error") else "generate_tests"


def _route_generate(state: AgentState) -> str:
    return "collect_results" if state.get("graph_error") else "validate"


def _route_validation(state: AgentState) -> str:
    """Route valid code to save, or failed code through bounded fix loop."""
    if state.get("graph_error"):
        return "collect_results"
    report = state.get("validation_report") or {}
    if bool(report.get("valid")):
        return "save_files"
    attempts = int(state.get("iteration", 0))
    max_attempts = int(state.get("max_iterations", 0))
    if attempts < max_attempts and state.get("repair_made", True):
        return "fix_tests"
    return "collect_results"


def _route_save(state: AgentState) -> str:
    if state.get("graph_error") or state.get("save_ok") is False:
        return "collect_results"
    return "run_ci"


def build_agent_graph():
    """Construct and compile a LangGraph state machine for one test generation run."""
    workflow = StateGraph(AgentState)
    timeout_seconds = get_settings().node_timeout_seconds
    workflow.add_node(
        "parse_requirement",
        _safe_node("parsing", parse_requirement_node),
        timeout=TimeoutPolicy(run_timeout=timeout_seconds),
    )
    workflow.add_node(
        "generate_tests",
        _safe_node("generating", generate_tests_node),
        timeout=TimeoutPolicy(run_timeout=timeout_seconds),
    )
    workflow.add_node(
        "validate",
        _safe_node("validating", validate_tests_node),
        timeout=TimeoutPolicy(run_timeout=timeout_seconds),
    )
    workflow.add_node(
        "fix_tests",
        _safe_node("fixing", fix_tests_node),
        timeout=TimeoutPolicy(run_timeout=timeout_seconds),
    )
    workflow.add_node(
        "save_files",
        _safe_node("saving", save_files_node),
        timeout=TimeoutPolicy(run_timeout=timeout_seconds),
    )
    workflow.add_node(
        "run_ci",
        _safe_node("ci_running", run_ci_node),
        timeout=TimeoutPolicy(run_timeout=timeout_seconds),
    )
    workflow.add_node(
        "collect_results",
        _safe_node("collecting", collect_results_node),
        timeout=TimeoutPolicy(run_timeout=timeout_seconds),
    )

    workflow.add_edge(START, "parse_requirement")
    workflow.add_conditional_edges(
        "parse_requirement",
        _route_parse,
        {"generate_tests": "generate_tests", "collect_results": "collect_results"},
    )
    workflow.add_conditional_edges(
        "generate_tests",
        _route_generate,
        {"validate": "validate", "collect_results": "collect_results"},
    )
    workflow.add_conditional_edges(
        "validate",
        _route_validation,
        {
            "save_files": "save_files",
            "fix_tests": "fix_tests",
            "collect_results": "collect_results",
        },
    )
    workflow.add_edge("fix_tests", "validate")
    workflow.add_conditional_edges(
        "save_files",
        _route_save,
        {"run_ci": "run_ci", "collect_results": "collect_results"},
    )
    workflow.add_edge("run_ci", "collect_results")
    workflow.add_edge("collect_results", END)
    return workflow.compile()


_AGENT_GRAPH = None


def get_agent_graph():
    """Return cached compiled LangGraph workflow."""
    global _AGENT_GRAPH
    if _AGENT_GRAPH is None:
        _AGENT_GRAPH = build_agent_graph()
    return _AGENT_GRAPH


async def stream_agent_events(initial_state: AgentState) -> AsyncIterator[Dict[str, Any]]:
    """Asynchronously stream node update events for live UI progress consumers."""
    graph = get_agent_graph()
    settings = get_settings()
    config = {
        "recursion_limit": max(25, int(initial_state.get("max_iterations", 0)) * 4 + 15),
        "configurable": {"node_timeout_seconds": settings.node_timeout_seconds},
    }
    async for event in graph.astream(initial_state, config=config, stream_mode="updates"):
        yield event


async def ainvoke_agent(initial_state: AgentState) -> Dict[str, Any]:
    """Asynchronously execute the agent graph with LangGraph's cooperative node timeouts."""
    graph = get_agent_graph()
    settings = get_settings()
    config = {
        "recursion_limit": max(25, int(initial_state.get("max_iterations", 0)) * 4 + 15),
        "configurable": {"node_timeout_seconds": settings.node_timeout_seconds},
    }
    return await graph.ainvoke(initial_state, config=config)


def invoke_agent(initial_state: AgentState) -> Dict[str, Any]:
    """Synchronous entrypoint used by FastAPI BackgroundTasks and EventBridge workers."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(ainvoke_agent(initial_state))
    raise RuntimeError(
        "invoke_agent cannot run inside an active event loop; await ainvoke_agent instead"
    )
