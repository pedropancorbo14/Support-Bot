from fastapi import APIRouter, Request, Response, status
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import DbSession
from app.llm import LlmClient

router = APIRouter(tags=["health"])


@router.get("/health")
async def health(
    request: Request,
    response: Response,
    session: DbSession,
) -> dict:
    """Comprueba la base de datos, Redis y el proveedor de LLM. Devuelve 503 si algo falla."""
    llm: LlmClient = request.app.state.llm
    checks = {
        "database": "ok" if await _database_ok(session) else "error",
        "redis": "ok" if await _redis_ok(request.app.state.redis) else "error",
        "llm": "ok" if await llm.ping() else "error",
    }
    healthy = all(value == "ok" for value in checks.values())
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ok" if healthy else "error", "checks": checks}


async def _redis_ok(redis: Redis) -> bool:
    try:
        return bool(await redis.ping())
    except RedisError:
        return False


async def _database_ok(session: AsyncSession) -> bool:
    try:
        await session.execute(text("SELECT 1"))
    except (SQLAlchemyError, OSError):
        return False
    return True
