import time

import httpx
import jwt
import pytest

from app.auth import decode_user_token
from tests.conftest import bearer, make_token

pytestmark = pytest.mark.anyio


async def test_valid_token_claims(demo_app, settings) -> None:
    client_app, _ = demo_app

    claims = decode_user_token(make_token(client_app), client_app, settings)

    assert claims["user_id"] == "u-42"
    assert claims["iss"] == "demo"


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"exp": int(time.time()) - 120}, jwt.ExpiredSignatureError),
        ({"iss": "otra-app"}, jwt.InvalidIssuerError),
        ({"user_id": None}, jwt.MissingRequiredClaimError),
        ({"iat": None}, jwt.MissingRequiredClaimError),
        ({"user_id": ""}, jwt.InvalidTokenError),
        # 30 min de vida: más que los 15 permitidos
        ({"iat": int(time.time()), "exp": int(time.time()) + 1800}, jwt.InvalidTokenError),
    ],
)
async def test_invalid_tokens(demo_app, settings, overrides, error) -> None:
    client_app, _ = demo_app

    with pytest.raises(error):
        decode_user_token(make_token(client_app, **overrides), client_app, settings)


async def test_wrong_secret_and_algorithm(demo_app, settings) -> None:
    client_app, _ = demo_app
    now = int(time.time())
    claims = {"iss": "demo", "user_id": "1", "iat": now, "exp": now + 60}

    with pytest.raises(jwt.InvalidSignatureError):
        decode_user_token(
            jwt.encode(claims, "otro-secreto-" + "x" * 40, "HS256"), client_app, settings
        )
    with pytest.raises(jwt.InvalidAlgorithmError):
        decode_user_token(jwt.encode(claims, client_app.jwt_secret, "HS512"), client_app, settings)
    unsigned = jwt.encode(claims, None, algorithm="none")
    with pytest.raises(jwt.InvalidTokenError):
        decode_user_token(unsigned, client_app, settings)


async def test_numeric_user_id_is_accepted(demo_app, settings) -> None:
    client_app, _ = demo_app

    claims = decode_user_token(make_token(client_app, user_id=42), client_app, settings)

    assert claims["user_id"] == 42


async def test_endpoint_rejects_missing_invalid_and_expired_tokens(client, demo_app) -> None:
    client_app, _ = demo_app
    expired = make_token(client_app, iat=int(time.time()) - 900, exp=int(time.time()) - 60)

    missing = await client.post("/v1/conversations")
    garbage = await client.post("/v1/conversations", headers=bearer("no-es-un-jwt"))
    unknown = await client.post(
        "/v1/conversations", headers=bearer(make_token(client_app, iss="nadie"))
    )
    old = await client.post("/v1/conversations", headers=bearer(expired))

    assert [r.status_code for r in (missing, garbage, unknown, old)] == [401] * 4
    assert old.json()["detail"] == "Token caducado"  # el widget lo renueva
    assert missing.headers["www-authenticate"] == "Bearer"


async def test_token_of_one_app_cannot_be_forged_for_another(
    client: httpx.AsyncClient, demo_app, other_app
) -> None:
    forged = jwt.encode(
        {"iss": "other", "user_id": "1", "iat": int(time.time()), "exp": int(time.time()) + 60},
        demo_app[0].jwt_secret,
        algorithm="HS256",
    )

    response = await client.post("/v1/conversations", headers=bearer(forged))

    assert response.status_code == 401
