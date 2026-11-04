"""Comandos de administración: `python -m app.cli <comando> --help`.

- `create-app`: da de alta una app y muestra su API key y sus secretos (JWT y webhook).
- `update-app`: cambia el webhook o los orígenes permitidos, o rota los secretos.
- `ingest`: indexa los `.md`/`.txt` de una carpeta para una app.
- `export-dataset`: exporta las respuestas valoradas con 👍 a JSONL (fine-tuning).
- `evaluate`: lanza los casos de `eval/cases.toml` contra el bot y muestra un informe.
"""

import argparse
import asyncio
import json
import re
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.auth import generate_jwt_secret, register_app
from app.chat.prompt import SystemPrompt
from app.config import Settings, get_settings
from app.dataset import export_dataset
from app.db import create_engine, create_sessionmaker
from app.evaluation import (
    format_report,
    load_cases,
    results_as_dicts,
    run_evaluation,
    summarize_results,
)
from app.llm import LlmClient, OpenAICompatibleClient
from app.models import ClientApp
from app.rag.ingest import upsert_document
from app.webhooks import generate_webhook_secret

DOC_EXTENSIONS = {".md", ".markdown", ".txt"}
_H1 = re.compile(r"^#\s+(.+?)\s*#*\s*$", re.MULTILINE)


def normalize_origin(value: str) -> str:
    """`https://app.com/ruta` → `https://app.com`. Lanza `ValueError` si no es un origen."""
    parts = urlsplit(value.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError(f"'{value}' no es un origen válido (ej. https://app.midominio.com)")
    return f"{parts.scheme}://{parts.netloc}".lower()


def parse_origins(values: Sequence[str] | None) -> list[str] | None:
    if values is None:
        return None
    return sorted({normalize_origin(v) for v in values if v.strip()})


async def create_app_command(session: AsyncSession, args: argparse.Namespace) -> int:
    if await session.scalar(select(ClientApp).where(ClientApp.slug == args.slug)):
        print(f"Ya existe una app con slug '{args.slug}'.", file=sys.stderr)
        return 1
    client_app, api_key = await register_app(session, slug=args.slug, name=args.name)
    client_app.webhook_url = args.webhook_url
    client_app.webhook_secret = generate_webhook_secret()
    client_app.allowed_origins = parse_origins(args.allowed_origin) or []
    await session.commit()
    print(f"App creada: {client_app.slug} (id {client_app.id})")
    print(f"Orígenes permitidos: {', '.join(client_app.allowed_origins) or '(ninguno)'}")
    print("Guarda estos secretos en la app cliente; no se volverán a mostrar.")
    print(f"API key: {api_key}")
    print(f"Secreto JWT: {client_app.jwt_secret}")
    print(f"Secreto del webhook: {client_app.webhook_secret}")
    return 0


async def update_app_command(session: AsyncSession, args: argparse.Namespace) -> int:
    client_app = await session.scalar(select(ClientApp).where(ClientApp.slug == args.slug))
    if client_app is None:
        print(f"No existe la app '{args.slug}'.", file=sys.stderr)
        return 1
    if args.webhook_url is not None:
        client_app.webhook_url = args.webhook_url or None
        print(f"Webhook: {client_app.webhook_url or '(desactivado)'}")
    if (origins := parse_origins(args.allowed_origin)) is not None:
        client_app.allowed_origins = origins
        print(f"Orígenes permitidos: {', '.join(origins) or '(ninguno)'}")
    if args.rotate_jwt_secret:
        client_app.jwt_secret = generate_jwt_secret()
        print(
            f"Nuevo secreto JWT (los tokens firmados con el anterior dejan de valer): "
            f"{client_app.jwt_secret}"
        )
    if args.rotate_webhook_secret:
        client_app.webhook_secret = generate_webhook_secret()
        print(f"Nuevo secreto del webhook: {client_app.webhook_secret}")
    await session.commit()
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


async def _find_app(session: AsyncSession, slug: str) -> ClientApp | None:
    client_app = await session.scalar(select(ClientApp).where(ClientApp.slug == slug))
    if client_app is None:
        print(f"No existe la app '{slug}'.", file=sys.stderr)
    return client_app


async def export_dataset_command(
    session: AsyncSession, args: argparse.Namespace, settings: Settings
) -> int:
    if (client_app := await _find_app(session, args.app)) is None:
        return 1
    since = datetime.fromisoformat(args.since) if args.since else None
    output = Path(args.output)
    with output.open("w", encoding="utf-8") as handle:  # noqa: ASYNC230 (CLI, fichero local)
        written = await export_dataset(
            session,
            client_app,
            SystemPrompt(settings.system_prompt_path),
            handle,
            history_max_messages=settings.history_max_messages,
            since=since,
        )
    print(f"{written} ejemplos exportados a {output}")
    return 0


async def evaluate_command(
    engine: AsyncEngine,
    session: AsyncSession,
    args: argparse.Namespace,
    settings: Settings,
    llm: LlmClient | None,
) -> int:
    if (client_app := await _find_app(session, args.app)) is None:
        return 1
    cases, markers = load_cases(Path(args.cases))
    own_llm = llm is None
    llm = llm or OpenAICompatibleClient.from_settings(settings)
    try:
        results = await run_evaluation(
            engine,
            llm,
            settings,
            SystemPrompt(settings.system_prompt_path),
            client_app,
            cases,
            markers,
        )
    finally:
        if own_llm:
            await llm.aclose()
    summary = summarize_results(results)
    print(format_report(results, summary))
    if args.output:
        report = {"summary": summary, "results": results_as_dicts(results)}
        Path(args.output).write_text(  # noqa: ASYNC240 (CLI, fichero local)
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"Informe guardado en {args.output}")
    if args.min_accuracy is not None and summary["accuracy"] < args.min_accuracy:
        print(f"Precisión por debajo de {args.min_accuracy:.0%}", file=sys.stderr)
        return 2
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
    create.add_argument("--webhook-url", help="URL que recibirá los webhooks.")
    create.add_argument(
        "--allowed-origin",
        action="append",
        help="Origen desde el que se carga el widget (repetible), p. ej. https://app.com.",
    )

    update = commands.add_parser("update-app", help="Modifica una app cliente.")
    update.add_argument("--slug", required=True, help="Slug de la app.")
    update.add_argument("--webhook-url", help='URL de los webhooks ("" para desactivarlos).')
    update.add_argument(
        "--allowed-origin",
        action="append",
        help='Sustituye los orígenes permitidos (repetible; "" para quitarlos todos).',
    )
    update.add_argument("--rotate-jwt-secret", action="store_true", help="Genera otro secreto JWT.")
    update.add_argument(
        "--rotate-webhook-secret", action="store_true", help="Genera otro secreto de webhook."
    )

    ingest = commands.add_parser("ingest", help="Indexa una carpeta de .md/.txt.")
    ingest.add_argument("--app", required=True, help="Slug de la app.")
    ingest.add_argument("folder", help="Carpeta con la documentación.")

    export = commands.add_parser(
        "export-dataset", help="Exporta las respuestas valoradas con 👍 a JSONL (Unsloth)."
    )
    export.add_argument("--app", required=True, help="Slug de la app.")
    export.add_argument("--output", required=True, help="Fichero .jsonl de salida.")
    export.add_argument("--since", help="Solo conversaciones desde esta fecha (ISO 8601).")

    evaluate = commands.add_parser("evaluate", help="Evalúa el bot con un fichero de casos.")
    evaluate.add_argument("--app", required=True, help="Slug de la app.")
    evaluate.add_argument("--cases", default="eval/cases.toml", help="Fichero de casos (TOML).")
    evaluate.add_argument("--output", help="Guarda el informe completo en JSON.")
    evaluate.add_argument(
        "--min-accuracy",
        type=float,
        help="Sale con código 2 si la precisión queda por debajo (0–1). Útil en CI.",
    )
    return parser


async def run(
    argv: Sequence[str] | None = None,
    settings: Settings | None = None,
    llm: LlmClient | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    settings = settings or get_settings()
    engine = create_engine(str(settings.database_url))
    try:
        async with create_sessionmaker(engine)() as session:
            match args.command:
                case "create-app":
                    return await create_app_command(session, args)
                case "update-app":
                    return await update_app_command(session, args)
                case "ingest":
                    return await ingest_command(session, args, settings)
                case "export-dataset":
                    return await export_dataset_command(session, args, settings)
                case _:
                    return await evaluate_command(engine, session, args, settings, llm)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
