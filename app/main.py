"""FastAPI application entrypoint and AWS Lambda/Mangum dispatch."""

from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from mangum import Mangum

from app.api.routes import router
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger, request_id_ctx_var
from app.db.session import init_db

settings = get_settings()
configure_logging(settings.log_level)
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Initialize database and logging for API process/container startup."""
    configure_logging(settings.log_level)
    init_db()
    logger.info("Application startup complete")
    yield
    logger.info("Application shutdown complete")


app = FastAPI(
    title=settings.app_name,
    description=(
        "Generate, validate, and deliver deterministic unit tests using a LangGraph agent. "
        "Generated code is security scanned before sandbox validation."
    ),
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# In production, set CORS_ORIGINS to the exact Streamlit/application origins.
_cors_origins = [
    origin.strip() for origin in os.getenv("CORS_ORIGINS", "*").split(",") if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins or ["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=[
        "Content-Type",
        "X-API-Key",
        "X-Request-ID",
        "X-Hub-Signature-256",
        "X-GitHub-Event",
    ],
    expose_headers=["X-Request-ID"],
)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    """Attach/propagate a request ID for logs and responses."""
    supplied = request.headers.get("X-Request-ID", "").strip()
    request_id = (
        supplied[:128]
        if supplied and all(ch.isalnum() or ch in "-_." for ch in supplied)
        else uuid.uuid4().hex
    )
    token = request_id_ctx_var.set(request_id)
    try:
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response
    except Exception:
        logger.exception("Unhandled request error", extra={"request_id": request_id})
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error", "request_id": request_id},
            headers={"X-Request-ID": request_id},
        )
    finally:
        request_id_ctx_var.reset(token)


app.include_router(router)


@app.get("/")
def root() -> Dict[str, str]:
    """Service metadata landing endpoint."""
    return {
        "name": settings.app_name,
        "version": "1.0.0",
        "docs": "/docs",
        "health": "/health",
    }


# Mangum manages the ASGI lifespan so SQLAlchemy tables are initialized by Lambda.
_asgi_handler = Mangum(app, lifespan="auto")


def handler(event: Dict[str, Any], context: Any) -> Any:
    """Lambda entrypoint dispatching API Gateway requests and weekly EventBridge events."""
    if event.get("automation") == "weekly-regression" or (
        event.get("source") == "aws.events" and event.get("detail-type") == "Scheduled Event"
    ):
        from app.automation import handle_scheduled_event

        return handle_scheduled_event(event)
    return _asgi_handler(event, context)
