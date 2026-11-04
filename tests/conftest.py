import os
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
import jwt
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth import register_app
from app.config import Settings
from app.db import create_engine, create_sessionmaker
from app.main import create_app
from app.models import ClientApp
from tests.fakes import FakeLlmClient

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://support:support@localhost:5432/support_test"
)
TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        database_url=TEST_DATABASE_URL,
        redis_url=TEST_REDIS_URL,
        cors_cache_seconds=0,
        widget_poll_interval=0.05,
    )


@pytest.fixture
def fake_llm() -> FakeLlmClient:
    return FakeLlmClient()


@pytest.fixture
async def sessionmaker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Sesiones contra la base de tests, vaciada al empezar cada test."""
    engine = create_engine(TEST_DATABASE_URL)
    async with engine.begin() as connection:
        await connection.execute(text("TRUNCATE apps RESTART IDENTITY CASCADE"))
    yield create_sessionmaker(engine)
    await engine.dispose()


@pytest.fixture
async def session(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with sessionmaker() as db_session:
        yield db_session


async def make_client_app(
    sessionmaker: async_sessionmaker[AsyncSession], slug: str
) -> tuple[ClientApp, str]:
    async with sessionmaker() as db_session:
        client_app, api_key = await register_app(db_session, slug=slug, name=slug.title())
        await db_session.commit()
    return client_app, api_key


@pytest.fixture
async def demo_app(sessionmaker: async_sessionmaker[AsyncSession]) -> tuple[ClientApp, str]:
    return await make_client_app(sessionmaker, "demo")


@pytest.fixture
async def other_app(sessionmaker: async_sessionmaker[AsyncSession]) -> tuple[ClientApp, str]:
    return await make_client_app(sessionmaker, "other")


@pytest.fixture
async def app(
    settings: Settings,
    fake_llm: FakeLlmClient,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[FastAPI]:
    application = create_app(settings, llm=fake_llm)
    async with application.router.lifespan_context(application):
        await application.state.redis.flushdb()
        yield application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


def make_token(client_app: ClientApp, **overrides: Any) -> str:
    """JWT de usuario final como lo emitiría la app cliente."""
    now = int(time.time())
    claims = {
        "iss": client_app.slug,
        "user_id": "u-42",
        "name": "Ana",
        "plan": "pro",
        "locale": "es-ES",
        "iat": now,
        "exp": now + 600,
    } | overrides
    claims = {key: value for key, value in claims.items() if value is not None}
    return jwt.encode(claims, client_app.jwt_secret, algorithm="HS256")


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
