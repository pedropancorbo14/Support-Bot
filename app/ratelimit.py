"""Rate limiting por usuario y por IP con Redis (ventana fija de un minuto).

Redis permite compartir los contadores entre varias instancias de la API. Si Redis no
responde, se deja pasar la petición y se registra un aviso: una caída de Redis no debe
tumbar el soporte.
"""

import logging
import time
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.auth import WidgetIdentity, require_end_user

logger = logging.getLogger(__name__)

WINDOW_SECONDS = 60


class RateLimiter:
    def __init__(self, redis: Redis, prefix: str = "ratelimit") -> None:
        self._redis = redis
        self._prefix = prefix

    async def hit(self, key: str, limit: int, now: float | None = None) -> int | None:
        """Cuenta una petición. Devuelve los segundos de espera si se supera `limit`."""
        now = time.time() if now is None else now
        window = int(now // WINDOW_SECONDS)
        redis_key = f"{self._prefix}:{key}:{window}"
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.incr(redis_key)
                pipe.expire(redis_key, WINDOW_SECONDS * 2)
                count, _ = await pipe.execute()
        except RedisError as exc:
            logger.warning("Rate limit desactivado temporalmente: Redis no responde (%s)", exc)
            return None
        if count > limit:
            return WINDOW_SECONDS - int(now % WINDOW_SECONDS)
        return None


def client_ip(request: Request) -> str:
    # Con --proxy-headers, uvicorn ya ha sustituido la IP por la de X-Forwarded-For
    # cuando el proxy está en FORWARDED_ALLOW_IPS.
    return request.client.host if request.client else "desconocida"


async def enforce_rate_limit(
    request: Request, identity: Annotated[WidgetIdentity, Depends(require_end_user)]
) -> WidgetIdentity:
    """Dependencia de los endpoints del widget: aplica ambos límites y devuelve la identidad."""
    settings = request.app.state.settings
    limiter: RateLimiter = request.app.state.rate_limiter
    checks = (
        (f"user:{identity.app.id}:{identity.user.user_id}", settings.rate_limit_user_per_minute),
        (f"ip:{client_ip(request)}", settings.rate_limit_ip_per_minute),
    )
    for key, limit in checks:
        if (retry_after := await limiter.hit(key, limit)) is not None:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Demasiadas peticiones. Inténtalo de nuevo en unos segundos.",
                headers={"Retry-After": str(retry_after)},
            )
    return identity


RateLimitedUser = Annotated[WidgetIdentity, Depends(enforce_rate_limit)]
