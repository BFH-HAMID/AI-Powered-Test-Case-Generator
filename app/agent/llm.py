"""LLM provider factory with local-synthesizer mode and optional LangChain integrations."""

from __future__ import annotations

from typing import Any, Optional

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


def _resolve_provider_and_model(model_name: Optional[str]) -> tuple[str, str]:
    """Resolve UI model overrides while preserving the backend's configured default provider."""
    settings = get_settings()
    configured_provider = (settings.llm_provider or "local-synthesizer").strip().lower()
    requested = (model_name or "configured").strip()
    normalized = requested.lower()
    if normalized in {"", "configured", "provider", "default"}:
        return configured_provider, settings.llm_model
    if normalized in {"local", "local-synthesizer", "offline", "none"}:
        return "local-synthesizer", "local-synthesizer"
    if normalized.startswith("ollama:"):
        return "ollama", requested.split(":", 1)[1]
    if normalized.startswith(("qwen", "llama", "deepseek", "mistral")):
        return "ollama", requested
    if normalized.startswith(("gpt-", "o1", "o3", "o4", "openai:")):
        return "openai", (
            requested.split(":", 1)[1] if normalized.startswith("openai:") else requested
        )
    if normalized.startswith(("claude", "anthropic:")):
        return "anthropic", (
            requested.split(":", 1)[1] if normalized.startswith("anthropic:") else requested
        )
    return configured_provider, requested


def get_chat_model(model_name: Optional[str] = None) -> Optional[Any]:
    """Return a configured LangChain chat model or None for deterministic local mode.

    Providers:
    - local-synthesizer: no network calls, deterministic built-in generation.
    - openai: uses OPENAI_API_KEY.
    - ollama: OpenAI-compatible endpoint at OLLAMA_BASE_URL.
    - anthropic: langchain-anthropic and ANTHROPIC_API_KEY.

    The UI can explicitly select a model family; the `configured` option uses the backend
    provider/model pair. Missing credentials always fall back to the local synthesizer.
    """
    settings = get_settings()
    provider, model = _resolve_provider_and_model(model_name)
    if provider in {"local", "local-synthesizer", "offline", "none"}:
        return None

    if provider == "openai":
        if not settings.openai_api_key:
            logger.warning("OpenAI selected without OPENAI_API_KEY; using local synthesizer")
            return None
        try:
            from langchain_openai import ChatOpenAI

            return ChatOpenAI(
                model=model,
                api_key=settings.openai_api_key,
                timeout=settings.llm_timeout_seconds,
                max_retries=1,
                temperature=0,
            )
        except Exception as exc:
            logger.warning(f"Could not initialize OpenAI model; using local synthesizer: {exc}")
            return None

    if provider == "ollama":
        try:
            from langchain_openai import ChatOpenAI

            base_url = settings.ollama_base_url.rstrip("/")
            if not base_url.endswith("/v1"):
                base_url += "/v1"
            return ChatOpenAI(
                model=model,
                base_url=base_url,
                api_key="ollama",
                timeout=settings.llm_timeout_seconds,
                max_retries=0,
                temperature=0,
            )
        except Exception as exc:
            logger.warning(
                f"Could not initialize Ollama-compatible model; using local synthesizer: {exc}"
            )
            return None

    if provider == "anthropic":
        if not settings.anthropic_api_key:
            logger.warning("Anthropic selected without ANTHROPIC_API_KEY; using local synthesizer")
            return None
        try:
            from langchain_anthropic import ChatAnthropic

            return ChatAnthropic(
                model=model,
                api_key=settings.anthropic_api_key,
                timeout=settings.llm_timeout_seconds,
                max_retries=1,
                temperature=0,
            )
        except Exception as exc:
            logger.warning(
                f"Could not initialize Anthropic model (install langchain-anthropic); "
                f"using local synthesizer: {exc}"
            )
            return None

    logger.warning(f"Unsupported LLM_PROVIDER '{provider}'; using local synthesizer")
    return None
