import pytest
from redis.asyncio import Redis

from app.ratelimit import RateLimiter
from tests.conftest import TEST_REDIS_URL

pytestmark = pytest.mark.anyio


@pytest.fixture
async def redis():
    client = Redis.from_url(TEST_REDIS_URL)
    await client.flushdb()
    yield client
    await client.aclose()


async def test_limit_within_window_and_reset_in_next_one(redis) -> None:
    limiter = RateLimiter(redis)
    start = 1_000_040.0  # 20 s dentro de una ventana de 60 s (1_000_020 es múltiplo de 60)

    results = [await limiter.hit("user:1", 2, now=start + i) for i in range(3)]
    next_window = await limiter.hit("user:1", 2, now=start + 40)

    assert results == [None, None, 38]  # Retry-After = segundos hasta la siguiente ventana
    assert next_window is None


async def test_keys_are_independent(redis) -> None:
    limiter = RateLimiter(redis)

    await limiter.hit("user:1", 1, now=0)

    assert await limiter.hit("user:2", 1, now=0) is None
    assert await limiter.hit("user:1", 1, now=0) is not None


async def test_fails_open_when_redis_is_down() -> None:
    down = Redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.2)
    limiter = RateLimiter(down)

    assert await limiter.hit("user:1", 0) is None
    await down.aclose()
