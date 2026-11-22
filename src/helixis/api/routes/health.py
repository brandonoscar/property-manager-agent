"""Health check endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@router.get("/health/ready")
async def readiness(request: Request) -> dict:
    """Check if all subsystems are ready."""
    checks = {}

    # Memory
    memory = request.app.state.memory_manager
    checks["memory"] = "ok" if memory and memory.available else "unavailable"

    # Composio
    composio = request.app.state.composio_client
    checks["composio"] = "ok" if composio and composio.available else "unavailable"

    # Event bus
    checks["event_bus"] = "ok"

    all_ok = all(v == "ok" for v in checks.values())

    settings = request.app.state.settings
    langfuse_enabled = bool(getattr(request.app.state, "langfuse_enabled", False))

    return {
        "status": "ready" if all_ok else "degraded",
        "checks": checks,
        "langfuse": {
            "enabled": langfuse_enabled,
            "host": settings.langfuse.host if langfuse_enabled else None,
        },
    }
