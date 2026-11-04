from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import CurrentApp
from app.db import get_session
from app.rag.ingest import IngestStatus, delete_document, upsert_document

router = APIRouter(prefix="/admin", tags=["admin"])

ExternalId = Annotated[str, Path(min_length=1, max_length=255)]


class DocumentIn(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    content: str = Field(min_length=1, description="Markdown o texto plano.")


class DocumentOut(BaseModel):
    external_id: str
    status: IngestStatus
    chunks: int
    pending_embeddings: int = Field(description="Chunks a la espera de que el worker los embeba.")


@router.put("/documents/{external_id}")
async def put_document(
    external_id: ExternalId,
    body: DocumentIn,
    client_app: CurrentApp,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> DocumentOut:
    """Crea o actualiza un documento de la app. Idempotente: si no cambia, no hace nada."""
    result = await upsert_document(
        session,
        client_app,
        external_id=external_id,
        title=body.title,
        content=body.content,
        settings=request.app.state.settings,
    )
    await session.commit()
    return DocumentOut(
        external_id=external_id,
        status=result.status,
        chunks=result.chunks,
        pending_embeddings=result.pending_embeddings,
    )


@router.delete("/documents/{external_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_document(
    external_id: ExternalId,
    client_app: CurrentApp,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    if not await delete_document(session, client_app, external_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Documento no encontrado")
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
