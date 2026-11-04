import httpx
import pytest

from app.config import Settings
from app.main import create_app
from tests.fakes import FakeLlmClient

pytestmark = pytest.mark.anyio


async def test_health_ok(client: httpx.AsyncClient) -> None:
    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "checks": {"database": "ok", "redis": "ok", "llm": "ok"},
    }


async def test_health_reports_llm_down(client: httpx.AsyncClient, fake_llm: FakeLlmClient) -> None:
    fake_llm.healthy = False

    response = await client.get("/health")

    assert response.status_code == 503
    assert response.json()["checks"] == {"database": "ok", "redis": "ok", "llm": "error"}


async def test_health_reports_database_and_redis_down() -> None:
    settings = Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://nobody:x@127.0.0.1:1/none",
        redis_url="redis://127.0.0.1:1/0",
    )
    app = create_app(settings, llm=FakeLlmClient())
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            response = await http.get("/health")

    assert response.status_code == 503
    assert response.json()["checks"] == {"database": "error", "redis": "error", "llm": "ok"}
