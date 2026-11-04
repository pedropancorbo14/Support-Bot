from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from redis.asyncio import Redis

from app.api import admin, health, widget
from app.chat.prompt import SystemPrompt
from app.config import Settings, get_settings
from app.cors import AppOriginsCORSMiddleware
from app.db import create_engine, create_sessionmaker
from app.llm import LlmClient, OpenAICompatibleClient
from app.ratelimit import RateLimiter

DESCRIPTION = """
Servicio de soporte con IA para una o varias apps cliente.

- **`/admin`**: para la app cliente (servidor a servidor). Cabecera `X-Api-Key`.
- **`/v1`**: para el widget. Cabecera `Authorization: Bearer <JWT>` emitido por la app.
"""


def create_app(settings: Settings | None = None, llm: LlmClient | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(str(settings.database_url))
        redis = Redis.from_url(settings.redis_url, socket_timeout=1, socket_connect_timeout=1)
        app.state.settings = settings
        app.state.sessionmaker = create_sessionmaker(engine)
        app.state.llm = llm or OpenAICompatibleClient.from_settings(settings)
        app.state.system_prompt = SystemPrompt(settings.system_prompt_path)
        app.state.redis = redis
        app.state.rate_limiter = RateLimiter(redis)
        try:
            yield
        finally:
            await app.state.llm.aclose()
            await redis.aclose()
            await engine.dispose()

    app = FastAPI(title="Support Bot", version="0.4.0", description=DESCRIPTION, lifespan=lifespan)
    app.add_middleware(AppOriginsCORSMiddleware, cache_seconds=settings.cors_cache_seconds)
    app.include_router(health.router)
    app.include_router(admin.router)
    app.include_router(widget.router)
    app.include_router(widget.static_router)
    return app


app = create_app()
