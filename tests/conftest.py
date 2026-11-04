import os
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI

from app.config import Settings
from app.main import create_app
from tests.fakes import FakeLlmClient

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://support:support@localhost:5432/support_test"
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, database_url=TEST_DATABASE_URL)


@pytest.fixture
def fake_llm() -> FakeLlmClient:
    return FakeLlmClient()


@pytest.fixture
async def app(settings: Settings, fake_llm: FakeLlmClient) -> AsyncIterator[FastAPI]:
    application = create_app(settings, llm=fake_llm)
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http
