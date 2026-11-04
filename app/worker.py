"""Worker en segundo plano: `python -m app.worker`.

Genera embeddings y envía webhooks. Usa PostgreSQL como cola (patrón outbox): un chunk con
`embedding IS NULL` o un webhook `pending` vencido es un trabajo pendiente. Se reclaman
con `FOR UPDATE SKIP LOCKED`, así que pueden correr varios workers a la vez y un trabajo
nunca se pierde aunque el worker caiga a mitad.
"""

import asyncio
import contextlib
import logging
import signal

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings, get_settings
from app.db import create_engine, create_sessionmaker
from app.llm import LlmClient, LlmError, OpenAICompatibleClient
from app.models import Chunk, Document
from app.webhooks import deliver_pending

logger = logging.getLogger("app.worker")

EMBEDDING_BATCH_SIZE = 32
MAX_BACKOFF_SECONDS = 60.0


def embedding_input(settings: Settings, title: str, section: str, content: str) -> str:
    """Texto que se embebe por cada chunk: incluye la ruta para dar contexto al vector."""
    source = f"{title} > {section}" if section else title
    return f"{settings.embedding_document_prefix}{source}\n{content}"


async def embed_pending_chunks(
    sessionmaker: async_sessionmaker[AsyncSession],
    llm: LlmClient,
    settings: Settings,
    batch_size: int = EMBEDDING_BATCH_SIZE,
) -> int:
    """Genera los embeddings de un lote de chunks pendientes. Devuelve cuántos procesó."""
    async with sessionmaker() as session, session.begin():
        rows = (
            await session.execute(
                select(Chunk, Document.title)
                .join(Document, Document.id == Chunk.document_id)
                .where(Chunk.embedding.is_(None))
                .order_by(Chunk.id)
                .limit(batch_size)
                .with_for_update(of=Chunk, skip_locked=True)
            )
        ).all()
        if not rows:
            return 0
        vectors = await llm.embed(
            [embedding_input(settings, title, c.section, c.content) for c, title in rows]
        )
        for (chunk, _), vector in zip(rows, vectors, strict=True):
            chunk.embedding = vector
    return len(rows)


async def run(settings: Settings, llm: LlmClient, stop: asyncio.Event) -> None:
    engine = create_engine(str(settings.database_url))
    sessionmaker = create_sessionmaker(engine)
    backoff = settings.worker_poll_interval
    async with httpx.AsyncClient(follow_redirects=False) as http:
        try:
            while not stop.is_set():
                # Los webhooks primero: un escalado no debe esperar a una reindexación.
                processed = await deliver_pending(sessionmaker, http, llm, settings)
                try:
                    embedded = await embed_pending_chunks(sessionmaker, llm, settings)
                except LlmError as exc:
                    logger.warning(
                        "Fallo generando embeddings (%s); reintento en %.1fs", exc, backoff
                    )
                    await _sleep(stop, backoff)
                    backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
                    continue
                backoff = settings.worker_poll_interval
                if embedded:
                    logger.info("Embeddings generados: %d", embedded)
                if processed:
                    logger.info("Webhooks procesados: %d", processed)
                if not embedded and not processed:
                    await _sleep(stop, settings.worker_poll_interval)
        finally:
            await engine.dispose()


async def _sleep(stop: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = get_settings()
    llm = OpenAICompatibleClient.from_settings(settings)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    logger.info("Worker iniciado")
    try:
        await run(settings, llm, stop)
    finally:
        await llm.aclose()
    logger.info("Worker detenido")


if __name__ == "__main__":
    asyncio.run(main())
