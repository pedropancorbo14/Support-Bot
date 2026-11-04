from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import admin, health
from app.config import Settings, get_settings
from app.db import create_engine, create_sessionmaker
from app.llm import LlmClient, OpenAICompatibleClient


def create_app(settings: Settings | None = None, llm: LlmClient | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(str(settings.database_url))
        app.state.settings = settings
        app.state.sessionmaker = create_sessionmaker(engine)
        app.state.llm = llm or OpenAICompatibleClient.from_settings(settings)
        try:
            yield
        finally:
            await app.state.llm.aclose()
            await engine.dispose()

    app = FastAPI(title="Support Bot", version="0.1.0", lifespan=lifespan)
    app.include_router(health.router)
    app.include_router(admin.router)
    return app


app = create_app()
