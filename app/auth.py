"""Autenticación.

- Apps cliente → servicio: cabecera `X-Api-Key` (endpoints `/admin`).
- Usuarios finales → servicio: JWT HS256 de corta duración emitido por la app cliente con
  su secreto (endpoints `/v1` del widget). El claim `iss` es el slug de la app.
"""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.prompt import EndUser
from app.config import Settings
from app.db import DbSession
from app.models import ClientApp

API_KEY_PREFIX = "sk_"

JWT_ALGORITHM = "HS256"

_api_key_header = APIKeyHeader(name="X-Api-Key", auto_error=False)
_bearer = HTTPBearer(auto_error=False, description="JWT del usuario final emitido por la app.")


def hash_api_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()


def generate_jwt_secret() -> str:
    return secrets.token_urlsafe(48)


async def register_app(session: AsyncSession, *, slug: str, name: str) -> tuple[ClientApp, str]:
    """Da de alta una app y devuelve su API key en claro (solo se muestra esta vez)."""
    api_key = API_KEY_PREFIX + secrets.token_urlsafe(32)
    client_app = ClientApp(
        slug=slug,
        name=name,
        api_key_hash=hash_api_key(api_key),
        jwt_secret=generate_jwt_secret(),
        allowed_origins=[],
    )
    session.add(client_app)
    await session.flush()
    return client_app, api_key


async def require_app(
    session: DbSession,
    api_key: Annotated[str | None, Depends(_api_key_header)],
) -> ClientApp:
    """Resuelve la app a partir de la cabecera `X-Api-Key`.

    Se guarda el SHA-256 de la clave; al buscar por hash, la comparación no filtra
    información por tiempo sobre la clave en claro.
    """
    if api_key:
        client_app = await session.scalar(
            select(ClientApp).where(ClientApp.api_key_hash == hash_api_key(api_key))
        )
        if client_app is not None:
            return client_app
    raise HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        detail="API key inválida o ausente",
        headers={"WWW-Authenticate": "ApiKey"},
    )


CurrentApp = Annotated[ClientApp, Depends(require_app)]


@dataclass(frozen=True, slots=True)
class WidgetIdentity:
    """Quién llama desde el widget: la app, el usuario y cuándo caduca su token."""

    app: ClientApp
    user: EndUser
    expires_at: datetime


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED, detail=detail, headers={"WWW-Authenticate": "Bearer"}
    )


def decode_user_token(token: str, client_app: ClientApp, settings: Settings) -> dict[str, Any]:
    """Valida firma, emisor, caducidad y duración máxima. Lanza `jwt.InvalidTokenError`."""
    claims = jwt.decode(
        token,
        client_app.jwt_secret,
        algorithms=[JWT_ALGORITHM],  # nunca aceptar el algoritmo que diga el token
        issuer=client_app.slug,
        leeway=settings.jwt_leeway_seconds,
        options={"require": ["exp", "iat", "iss", "user_id"]},
    )
    if claims["exp"] - claims["iat"] > settings.jwt_max_ttl_seconds:
        raise jwt.InvalidTokenError("La vida del token supera el máximo permitido")
    if not isinstance(claims["user_id"], str | int) or str(claims["user_id"]) == "":
        raise jwt.InvalidTokenError("user_id inválido")
    return claims


def _optional_str(claims: dict[str, Any], key: str) -> str | None:
    value = claims.get(key)
    return str(value) if value not in (None, "") else None


async def require_end_user(
    request: Request,
    session: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> WidgetIdentity:
    if credentials is None:
        raise _unauthorized("Falta el token")
    try:
        issuer = jwt.decode(credentials.credentials, options={"verify_signature": False}).get("iss")
    except jwt.InvalidTokenError:
        raise _unauthorized("Token inválido") from None
    client_app = (
        await session.scalar(select(ClientApp).where(ClientApp.slug == issuer))
        if isinstance(issuer, str)
        else None
    )
    if client_app is None:
        raise _unauthorized("Token inválido")
    try:
        claims = decode_user_token(credentials.credentials, client_app, request.app.state.settings)
    except jwt.ExpiredSignatureError:
        raise _unauthorized("Token caducado") from None
    except jwt.InvalidTokenError:
        raise _unauthorized("Token inválido") from None

    origin = request.headers.get("origin")
    if origin is not None and origin not in client_app.allowed_origins:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Origen no permitido para esta app")

    return WidgetIdentity(
        app=client_app,
        user=EndUser(
            user_id=str(claims["user_id"]),
            name=_optional_str(claims, "name"),
            plan=_optional_str(claims, "plan"),
            locale=_optional_str(claims, "locale"),
        ),
        expires_at=datetime.fromtimestamp(claims["exp"], UTC),
    )


CurrentUser = Annotated[WidgetIdentity, Depends(require_end_user)]
