import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.llm import LlmError
from app.models import Chunk, ClientApp, Document
from app.rag.ingest import IngestStatus, delete_document, upsert_document
from app.worker import embed_pending_chunks
from tests.fakes import FakeLlmClient

pytestmark = pytest.mark.anyio

DOC = "## Pagos\n\nAceptamos tarjeta.\n\n## Envíos\n\nEnviamos en 24 horas."


async def upsert(
    session: AsyncSession, client_app: ClientApp, settings: Settings, content: str = DOC, **kw
):
    result = await upsert_document(
        session,
        client_app,
        external_id=kw.get("external_id", "faq"),
        title=kw.get("title", "FAQ"),
        content=content,
        settings=settings,
    )
    await session.commit()
    return result


async def chunks_of(session: AsyncSession, client_app: ClientApp) -> list[Chunk]:
    session.expire_all()
    return list(
        await session.scalars(
            select(Chunk).where(Chunk.app_id == client_app.id).order_by(Chunk.position)
        )
    )


async def test_create_stores_chunks_pending_embedding(session, demo_app, settings) -> None:
    client_app, _ = demo_app

    result = await upsert(session, client_app, settings)

    assert (result.status, result.chunks, result.pending_embeddings) == (
        IngestStatus.CREATED,
        2,
        2,
    )
    chunks = await chunks_of(session, client_app)
    assert [(c.section, c.content) for c in chunks] == [
        ("Pagos", "Aceptamos tarjeta."),
        ("Envíos", "Enviamos en 24 horas."),
    ]
    assert all(c.embedding is None for c in chunks)


async def test_reingest_same_content_is_noop(session, demo_app, settings) -> None:
    client_app, _ = demo_app
    await upsert(session, client_app, settings)

    result = await upsert(session, client_app, settings)

    assert result.status is IngestStatus.UNCHANGED


async def test_update_only_reembeds_changed_chunks(
    session, sessionmaker, demo_app, settings, fake_llm: FakeLlmClient
) -> None:
    client_app, _ = demo_app
    await upsert(session, client_app, settings)
    await embed_pending_chunks(sessionmaker, fake_llm, settings)
    before = {c.section: c.id for c in await chunks_of(session, client_app)}

    changed = DOC.replace("24 horas", "48 horas") + "\n\n## Devoluciones\n\n30 días."
    result = await upsert(session, client_app, settings, content=changed)

    assert (result.status, result.chunks, result.pending_embeddings) == (
        IngestStatus.UPDATED,
        3,
        2,
    )
    chunks = await chunks_of(session, client_app)
    pagos = chunks[0]
    assert pagos.id == before["Pagos"]  # se conserva la fila y su embedding
    assert pagos.embedding is not None
    assert [c.embedding is None for c in chunks] == [False, True, True]
    assert await session.scalar(select(func.count()).select_from(Chunk)) == 3  # sin huérfanos


async def test_title_change_invalidates_all_embeddings(
    session, sessionmaker, demo_app, settings, fake_llm
) -> None:
    client_app, _ = demo_app
    await upsert(session, client_app, settings)
    await embed_pending_chunks(sessionmaker, fake_llm, settings)

    result = await upsert(session, client_app, settings, title="Preguntas frecuentes")

    assert result.pending_embeddings == 2


async def test_delete_document_removes_chunks(session, demo_app, settings) -> None:
    client_app, _ = demo_app
    await upsert(session, client_app, settings)

    assert await delete_document(session, client_app, "faq") is True
    await session.commit()

    assert await chunks_of(session, client_app) == []
    assert await delete_document(session, client_app, "faq") is False


async def test_same_external_id_is_independent_per_app(
    session, demo_app, other_app, settings
) -> None:
    await upsert(session, demo_app[0], settings)
    await upsert(session, other_app[0], settings, content="Otra cosa.")

    await delete_document(session, other_app[0], "faq")
    await session.commit()

    assert len(await chunks_of(session, demo_app[0])) == 2
    assert await session.scalar(select(func.count()).select_from(Document)) == 1


async def test_worker_embeds_with_document_prefix_and_source(
    session, sessionmaker: async_sessionmaker, demo_app, settings, fake_llm
) -> None:
    await upsert(session, demo_app[0], settings)

    assert await embed_pending_chunks(sessionmaker, fake_llm, settings) == 2
    assert await embed_pending_chunks(sessionmaker, fake_llm, settings) == 0

    assert fake_llm.embedded == [
        "search_document: FAQ > Pagos\nAceptamos tarjeta.",
        "search_document: FAQ > Envíos\nEnviamos en 24 horas.",
    ]
    assert all(c.embedding is not None for c in await chunks_of(session, demo_app[0]))


async def test_worker_failure_keeps_chunks_pending(
    session, sessionmaker, demo_app, settings
) -> None:
    await upsert(session, demo_app[0], settings)

    with pytest.raises(LlmError):
        await embed_pending_chunks(sessionmaker, FakeLlmClient(fail=True), settings)

    assert all(c.embedding is None for c in await chunks_of(session, demo_app[0]))
