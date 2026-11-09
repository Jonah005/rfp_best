from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import text

from app.core.config import get_settings
from app.schemas.common import HealthOut

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live", response_model=HealthOut)
def live() -> HealthOut:
    return HealthOut(status="ok", checks={"process": "ok"})


@router.get("/ready", response_model=HealthOut)
def ready() -> HealthOut:
    checks: dict[str, str] = {}
    settings = get_settings()

    try:
        from app.db.session import get_engine

        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:  # pragma: no cover - environment dependent
        checks["database"] = f"error: {type(exc).__name__}"

    try:
        import redis

        redis.Redis.from_url(settings.redis_url, socket_connect_timeout=2).ping()
        checks["redis"] = "ok"
    except Exception as exc:  # pragma: no cover
        checks["redis"] = f"error: {type(exc).__name__}"

    try:
        from app.services.storage import get_storage

        get_storage().health()
        checks["storage"] = "ok"
    except Exception as exc:  # pragma: no cover
        checks["storage"] = f"error: {type(exc).__name__}"

    checks["llm"] = "configured" if settings.deepseek_api_key else "unconfigured"
    checks["embeddings"] = settings.embedding_provider

    status = (
        "ok"
        if all(v == "ok" for k, v in checks.items() if k in ("database", "redis", "storage"))
        else "degraded"
    )
    return HealthOut(status=status, checks=checks)
