import pytest

from app.rag.ingest import upsert_document
from app.rag.retriever import NO_CONTEXT, RetrievedChunk, format_context, retrieve
from app.worker import embed_pending_chunks

pytestmark = pytest.mark.anyio

FAQ = """## Pagos

Aceptamos tarjeta de crédito y transferencia bancaria.

## Envíos

Los pedidos se envían en 24 horas por mensajería urgente.

## Cuenta

Puedes cambiar tu contraseña desde el perfil de usuario.
"""


@pytest.fixture
async def indexed(session, sessionmaker, demo_app, other_app, settings, fake_llm):
    for client_app, content in ((demo_app[0], FAQ), (other_app[0], "## Pagos\n\nSolo bitcoin.")):
        await upsert_document(
            session, client_app, external_id="faq", title="FAQ", content=content, settings=settings
        )
    await session.commit()
    await embed_pending_chunks(sessionmaker, fake_llm, settings)


async def test_retrieves_most_similar_chunk_first(
    indexed, session, demo_app, settings, fake_llm
) -> None:
    chunks = await retrieve(
        session,
        fake_llm,
        app_id=demo_app[0].id,
        query="¿en cuántas horas se envían los pedidos?",
        settings=settings.model_copy(update={"rag_min_score": 0.0}),
    )

    assert chunks[0].section == "Envíos"
    assert chunks == sorted(chunks, key=lambda c: c.score, reverse=True)
    assert len(chunks) == 3
    assert fake_llm.embedded[-1].startswith("search_query: ")


async def test_top_k_limits_results(indexed, session, demo_app, settings, fake_llm) -> None:
    chunks = await retrieve(
        session,
        fake_llm,
        app_id=demo_app[0].id,
        query="pedidos",
        settings=settings.model_copy(update={"rag_top_k": 1, "rag_min_score": 0.0}),
    )

    assert len(chunks) == 1


async def test_threshold_filters_out_irrelevant_chunks(
    indexed, session, demo_app, settings, fake_llm
) -> None:
    chunks = await retrieve(
        session,
        fake_llm,
        app_id=demo_app[0].id,
        query="receta de paella valenciana",
        settings=settings.model_copy(update={"rag_min_score": 0.3}),
    )

    assert chunks == []
    assert format_context(chunks) == NO_CONTEXT


async def test_never_returns_chunks_from_other_apps(
    indexed, session, other_app, settings, fake_llm
) -> None:
    chunks = await retrieve(
        session,
        fake_llm,
        app_id=other_app[0].id,
        query="aceptamos tarjeta de crédito",
        settings=settings.model_copy(update={"rag_min_score": 0.0}),
    )

    assert [c.content for c in chunks] == ["Solo bitcoin."]


def test_format_context() -> None:
    chunks = [
        RetrievedChunk(1, "FAQ", "Pagos > Tarjeta", "Visa.", 0.9),
        RetrievedChunk(2, "Guía", "", "Hola.", 0.8),
    ]

    assert format_context(chunks) == "[FAQ > Pagos > Tarjeta] Visa.\n\n[Guía] Hola."
