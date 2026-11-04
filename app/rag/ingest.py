"""Alta, actualización y borrado de documentos.

La ingesta es síncrona y barata: trocea, compara hashes y guarda los chunks. Los chunks
nuevos o modificados quedan con `embedding = NULL` y el worker los procesa después, así
que la petición nunca espera al modelo de embeddings.
"""

import hashlib
from collections import defaultdict
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import Settings
from app.models import Chunk, ClientApp, Document
from app.rag.chunker import chunk_document


class IngestStatus(StrEnum):
    CREATED = "created"
    UPDATED = "updated"
    UNCHANGED = "unchanged"


@dataclass(frozen=True, slots=True)
class IngestResult:
    status: IngestStatus
    chunks: int
    pending_embeddings: int


async def upsert_document(
    session: AsyncSession,
    client_app: ClientApp,
    *,
    external_id: str,
    title: str,
    content: str,
    settings: Settings,
) -> IngestResult:
    document_hash = hashlib.sha256(f"{title}\n{content}".encode()).hexdigest()
    document = await session.scalar(
        select(Document)
        .where(Document.app_id == client_app.id, Document.external_id == external_id)
        .options(selectinload(Document.chunks))
        .with_for_update()
    )
    if document is not None and document.content_hash == document_hash:
        pending = sum(chunk.embedding is None for chunk in document.chunks)
        return IngestResult(IngestStatus.UNCHANGED, len(document.chunks), pending)

    status = IngestStatus.UPDATED
    if document is None:
        status = IngestStatus.CREATED
        document = Document(app_id=client_app.id, external_id=external_id, chunks=[])
        session.add(document)
    document.title = title
    document.content_hash = document_hash

    drafts = chunk_document(
        title,
        content,
        max_tokens=settings.chunk_size_tokens,
        overlap_tokens=settings.chunk_overlap_tokens,
    )

    # Reutiliza los chunks cuyo hash no ha cambiado para conservar su embedding.
    reusable: dict[str, list[Chunk]] = defaultdict(list)
    for chunk in document.chunks:
        reusable[chunk.content_hash].append(chunk)

    chunks: list[Chunk] = []
    for draft in drafts:
        if reusable[draft.content_hash]:
            chunk = reusable[draft.content_hash].pop()
            chunk.position = draft.position
        else:
            chunk = Chunk(
                app_id=client_app.id,
                position=draft.position,
                section=draft.section,
                content=draft.content,
                content_hash=draft.content_hash,
                token_count=draft.token_count,
            )
        chunks.append(chunk)
    document.chunks = chunks  # delete-orphan elimina los que sobran
    await session.flush()

    pending = sum(chunk.embedding is None for chunk in chunks)
    return IngestResult(status, len(chunks), pending)


async def delete_document(session: AsyncSession, client_app: ClientApp, external_id: str) -> bool:
    result = await session.execute(
        delete(Document).where(
            Document.app_id == client_app.id, Document.external_id == external_id
        )
    )
    return result.rowcount > 0
