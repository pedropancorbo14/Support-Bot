from pathlib import Path

import pytest
from sqlalchemy import select

from app import cli
from app.auth import hash_api_key
from app.models import ClientApp, Document

pytestmark = pytest.mark.anyio


async def test_create_app_prints_api_key_once_and_stores_hash(
    sessionmaker, settings, capsys
) -> None:
    code = await cli.run(["create-app", "--slug", "tienda", "--name", "Tienda"], settings)

    output = capsys.readouterr().out
    api_key = output.split("API key: ")[1].split()[0]
    async with sessionmaker() as session:
        client_app = await session.scalar(select(ClientApp))
    assert code == 0
    assert api_key.startswith("sk_")
    assert client_app.slug == "tienda"
    assert client_app.api_key_hash == hash_api_key(api_key)


async def test_create_app_rejects_duplicate_slug(sessionmaker, demo_app, settings, capsys) -> None:
    code = await cli.run(["create-app", "--slug", "demo", "--name", "Demo"], settings)

    assert code == 1
    assert "Ya existe" in capsys.readouterr().err


async def test_ingest_folder(sessionmaker, demo_app, settings, tmp_path: Path, capsys) -> None:
    (tmp_path / "guias").mkdir()
    (tmp_path / "guias" / "pagos.md").write_text("# Cómo pagar\n\n## Tarjeta\n\nVisa.")
    (tmp_path / "preguntas-frecuentes.txt").write_text("¿Horario? De 9 a 18.")
    (tmp_path / "manual.pdf").write_bytes(b"%PDF")  # formato no soportado: se ignora

    code = await cli.run(["ingest", "--app", "demo", str(tmp_path)], settings)

    async with sessionmaker() as session:
        documents = {d.external_id: d.title for d in await session.scalars(select(Document))}
    assert code == 0
    assert documents == {
        "guias/pagos.md": "Cómo pagar",
        "preguntas-frecuentes.txt": "Preguntas frecuentes",
    }
    assert "2 documentos procesados" in capsys.readouterr().out


async def test_ingest_unknown_app(sessionmaker, settings, tmp_path: Path, capsys) -> None:
    assert await cli.run(["ingest", "--app", "nadie", str(tmp_path)], settings) == 1
    assert "No existe la app" in capsys.readouterr().err


async def test_create_app_with_webhook_shows_secret(sessionmaker, settings, capsys) -> None:
    await cli.run(
        ["create-app", "--slug", "t", "--name", "T", "--webhook-url", "https://t.example/hook"],
        settings,
    )

    output = capsys.readouterr().out
    secret = output.split("Secreto del webhook: ")[1].split()[0]
    async with sessionmaker() as session:
        client_app = await session.scalar(select(ClientApp))
    assert secret.startswith("whsec_")
    assert (client_app.webhook_url, client_app.webhook_secret) == ("https://t.example/hook", secret)


async def test_update_app_webhook_url(sessionmaker, demo_app, settings, capsys) -> None:
    assert (
        await cli.run(["update-app", "--slug", "demo", "--webhook-url", "https://x/h"], settings)
        == 0
    )
    assert await cli.run(["update-app", "--slug", "demo", "--webhook-url", ""], settings) == 0
    assert await cli.run(["update-app", "--slug", "nadie", "--webhook-url", "x"], settings) == 1

    async with sessionmaker() as session:
        client_app = await session.scalar(select(ClientApp))
    assert client_app.webhook_url is None
    assert "(desactivado)" in capsys.readouterr().out
