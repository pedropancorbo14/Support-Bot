"""CORS con los orígenes configurados por cada app.

El preflight (`OPTIONS`) no lleva el JWT, así que no se sabe de qué app es. Se acepta
si el origen pertenece a **alguna** app. Después, `require_end_user` comprueba que el
origen pertenece a la app del token. Los orígenes se cachean unos segundos.
"""

import time

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

from app.models import ClientApp

WIDGET_PATH_PREFIX = "/v1/"


class AppOriginsCORSMiddleware(CORSMiddleware):
    def __init__(self, app: ASGIApp, *, cache_seconds: float) -> None:
        super().__init__(
            app,
            allow_methods=["GET", "POST"],
            allow_headers=["Authorization", "Content-Type", "Last-Event-ID"],
            max_age=600,
        )
        self._cache_seconds = cache_seconds
        self._origins: frozenset[str] = frozenset()
        self._loaded_at = float("-inf")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith(WIDGET_PATH_PREFIX):
            await self.app(scope, receive, send)  # admin y health: sin CORS
            return
        await self._refresh(scope["app"].state.sessionmaker)
        await super().__call__(scope, receive, send)

    def is_allowed_origin(self, origin: str) -> bool:
        return origin in self._origins

    async def _refresh(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        if time.monotonic() - self._loaded_at < self._cache_seconds:
            return
        async with sessionmaker() as session:
            rows = await session.scalars(select(func.unnest(ClientApp.allowed_origins)))
            self._origins = frozenset(rows)
        self._loaded_at = time.monotonic()
