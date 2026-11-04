"""Comandos de administración: `python -m app.cli <comando>`.

create-app --slug mi-app --name "Mi App"    Da de alta una app y muestra su API key.
ingest --app mi-app ./docs                  Indexa los .md/.txt de una carpeta.
"""

import argparse
import asyncio
import re
import sys
from collections.abc import Sequence
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import register_app
from app.config import Settings, get_settings
from app.db import create_engine, create_sessionmaker
from app.models import ClientApp
from app.rag.ingest import upsert_document

DOC_EXTENSIONS = {".md", ".markdown", ".txt"}
_H1 = re.compile(r"^#\s+(.+?)\s*#*\s*$", re.MULTILINE)


async def create_app_command(session: AsyncSession, args: argparse.Namespace) -> int:
    if await session.scalar(select(ClientApp).where(ClientApp.slug == args.slug)):
        print(f"Ya existe una app con slug '{args.slug}'.", file=sys.stderr)
        return 1
    client_app, api_key = await register_app(session, slug=args.slug, name=args.name)
    await session.commit()
    print(f"App creada: {client_app.slug} (id {client_app.id})")
    print(f"API key (guárdala, no se volverá a mostrar): {api_key}")
    return 0


async def ingest_command(
    session: AsyncSession, args: argparse.Namespace, settings: Settings
) -> int:
    client_app = await session.scalar(select(ClientApp).where(ClientApp.slug == args.app))
    if client_app is None:
        print(f"No existe la app '{args.app}'.", file=sys.stderr)
        return 1
    root = Path(args.folder)
    try:
        documents = await asyncio.to_thread(read_documents, root)
    except NotADirectoryError:
        print(f"'{root}' no es una carpeta.", file=sys.stderr)
        return 1

    for external_id, title, content in documents:
        result = await upsert_document(
            session,
            client_app,
            external_id=external_id,
            title=title,
            content=content,
            settings=settings,
        )
        await session.commit()
        print(f"{result.status:>9}  {external_id}  ({result.chunks} chunks)")
    print(f"{len(documents)} documentos procesados. El worker generará los embeddings pendientes.")
    return 0


def read_documents(root: Path) -> list[tuple[str, str, str]]:
    """(external_id, título, contenido) de cada .md/.txt; el id es la ruta relativa."""
    if not root.is_dir():
        raise NotADirectoryError(root)
    paths = sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in DOC_EXTENSIONS)
    documents = []
    for path in paths:
        content = path.read_text(encoding="utf-8")
        documents.append(
            (path.relative_to(root).as_posix(), document_title(path, content), content)
        )
    return documents


def document_title(path: Path, content: str) -> str:
    """Primer H1 del documento o, si no hay, el nombre del fichero."""
    if path.suffix.lower() != ".txt" and (match := _H1.search(content)):
        return match.group(1).strip()
    return path.stem.replace("-", " ").replace("_", " ").strip().capitalize()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create-app", help="Da de alta una app cliente.")
    create.add_argument("--slug", required=True, help="Identificador corto y único.")
    create.add_argument("--name", required=True, help="Nombre visible.")

    ingest = commands.add_parser("ingest", help="Indexa una carpeta de .md/.txt.")
    ingest.add_argument("--app", required=True, help="Slug de la app.")
    ingest.add_argument("folder", help="Carpeta con la documentación.")
    return parser


async def run(argv: Sequence[str] | None = None, settings: Settings | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = settings or get_settings()
    engine = create_engine(str(settings.database_url))
    try:
        async with create_sessionmaker(engine)() as session:
            if args.command == "create-app":
                return await create_app_command(session, args)
            return await ingest_command(session, args, settings)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
