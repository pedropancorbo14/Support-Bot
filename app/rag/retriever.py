"""Recuperación de fragmentos por similitud de coseno."""

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.llm import LlmClient
from app.models import Chunk, Document

NO_CONTEXT = "No hay documentación relevante para esta consulta"


@dataclass(frozen=True, slots=True)
class RetrievedChunk:
    chunk_id: int
    document_title: str
    section: str
    content: str
    score: float

    def format(self) -> str:
        source = f"{self.document_title} > {self.section}" if self.section else self.document_title
        return f"[{source}] {self.content}"


async def retrieve(
    session: AsyncSession,
    llm: LlmClient,
    *,
    app_id: int,
    query: str,
    settings: Settings,
) -> list[RetrievedChunk]:
    """Top-k chunks de la app más similares a `query` que superan `rag_min_score`."""
    [vector] = await llm.embed([settings.embedding_query_prefix + query])
    distance = Chunk.embedding.cosine_distance(vector)
    rows = await session.execute(
        select(Chunk.id, Document.title, Chunk.section, Chunk.content, distance.label("distance"))
        .join(Document, Document.id == Chunk.document_id)
        .where(Chunk.app_id == app_id, Chunk.embedding.is_not(None))
        .order_by(distance)
        .limit(settings.rag_top_k)
    )
    # El umbral se aplica después del ORDER BY ... LIMIT para que use el índice HNSW.
    results = [
        RetrievedChunk(
            chunk_id=row.id,
            document_title=row.title,
            section=row.section,
            content=row.content,
            score=1.0 - row.distance,
        )
        for row in rows
    ]
    return [chunk for chunk in results if chunk.score >= settings.rag_min_score]


def format_context(chunks: Sequence[RetrievedChunk]) -> str:
    if not chunks:
        return NO_CONTEXT
    return "\n\n".join(chunk.format() for chunk in chunks)
