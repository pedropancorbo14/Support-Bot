"""Autenticación de las apps cliente."""

import hashlib
import secrets
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import APIKeyHeader
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import ClientApp

API_KEY_PREFIX = "sk_"

_api_key_header = APIKeyHeader(name="X-Api-Key", auto_error=False)


def hash_api_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()


async def register_app(session: AsyncSession, *, slug: str, name: str) -> tuple[ClientApp, str]:
    """Da de alta una app y devuelve su API key en claro (solo se muestra esta vez)."""
    api_key = API_KEY_PREFIX + secrets.token_urlsafe(32)
    client_app = ClientApp(slug=slug, name=name, api_key_hash=hash_api_key(api_key))
    session.add(client_app)
    await session.flush()
    return client_app, api_key


async def require_app(
    session: Annotated[AsyncSession, Depends(get_session)],
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
