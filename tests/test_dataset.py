import io
import json
from pathlib import Path

import pytest

from app import cli
from app.chat.prompt import EndUser, SystemPrompt
from app.chat.service import ChatService
from app.dataset import export_dataset
from app.rag.ingest import upsert_document
from app.worker import embed_pending_chunks
from tests.fakes import FakeLlmClient

pytestmark = pytest.mark.anyio


async def collect(events) -> list:
    return [e async for e in events]


@pytest.fixture
def prompt(tmp_path: Path) -> SystemPrompt:
    path = tmp_path / "system.md"
    path.write_text("Eres el asistente de {app_name}.")
    return SystemPrompt(path)


@pytest.fixture
async def rated(session, sessionmaker, demo_app, settings, prompt):
    client_app, _ = demo_app
    await upsert_document(
        session,
        client_app,
        external_id="faq",
        title="FAQ",
        content="## Pagos\n\nAceptamos tarjeta de crédito.",
        settings=settings,
    )
    await session.commit()
    await embed_pending_chunks(sessionmaker, FakeLlmClient(), settings)
    settings = settings.model_copy(update={"rag_min_score": 0.3})
    llm = FakeLlmClient(["Hola, Ana.", "Sí, aceptamos tarjeta.", "Ni idea."])
    chat = ChatService(session, llm, settings, prompt)
    conversation = await chat.start_conversation(client_app, EndUser("u1", "Ana", "pro", "es"))
    await collect(chat.reply(client_app, conversation, "Hola"))
    await collect(chat.reply(client_app, conversation, "aceptamos tarjeta de crédito"))
    await collect(chat.reply(client_app, conversation, "¿y bitcoin?"))
    _, hello, _, card, _, unknown = await chat.messages(conversation)
    await chat.rate(client_app.id, "u1", card.id, 1)
    await chat.rate(client_app.id, "u1", unknown.id, -1)  # mal valorada: no se exporta

    # Escalado bien valorado: la etiqueta se conserva para que el modelo aprenda a escalar.
    escalating = ChatService(
        session, FakeLlmClient(["Te paso. [ESCALAR: reembolso]"]), settings, prompt
    )
    other = await escalating.start_conversation(client_app, EndUser("u2"))
    await collect(escalating.reply(client_app, other, "Quiero un reembolso"))
    [_, answer] = await escalating.messages(other)
    await escalating.rate(client_app.id, "u2", answer.id, 1)
    return client_app


async def test_exports_only_well_rated_answers_with_rag_context(
    session, rated, settings, prompt
) -> None:
    output = io.StringIO()

    written = await export_dataset(session, rated, prompt, output, history_max_messages=10)

    lines = [json.loads(line) for line in output.getvalue().splitlines()]
    assert written == len(lines) == 2
    card, escalation = (sample["messages"] for sample in lines)

    assert [m["role"] for m in card] == ["system", "user", "assistant", "user", "assistant"]
    assert card[0]["content"].startswith("Eres el asistente de Demo.\n\n## Datos del usuario")
    assert "- Nombre: Ana" in card[0]["content"]
    assert card[1:3] == [
        {"role": "user", "content": "Hola"},
        {"role": "assistant", "content": "Hola, Ana."},
    ]
    assert card[3]["content"] == (
        "## Documentación\n[FAQ > Pagos] Aceptamos tarjeta de crédito.\n\n"
        "## Pregunta\naceptamos tarjeta de crédito"
    )
    assert card[4] == {"role": "assistant", "content": "Sí, aceptamos tarjeta."}

    assert escalation[-1]["content"] == "Te paso. [ESCALAR: reembolso]"
    assert escalation[-2]["content"].startswith(
        "## Documentación\nNo hay documentación relevante para esta consulta"
    )


async def test_history_can_be_disabled(session, rated, prompt) -> None:
    output = io.StringIO()

    await export_dataset(session, rated, prompt, output, history_max_messages=0)

    first = json.loads(output.getvalue().splitlines()[0])["messages"]
    assert [m["role"] for m in first] == ["system", "user", "assistant"]


async def test_export_is_scoped_to_app(session, rated, other_app, prompt) -> None:
    output = io.StringIO()

    assert await export_dataset(session, other_app[0], prompt, output, history_max_messages=10) == 0


async def test_cli_export(sessionmaker, rated, settings, tmp_path: Path, capsys) -> None:
    target = tmp_path / "dataset.jsonl"

    code = await cli.run(["export-dataset", "--app", "demo", "--output", str(target)], settings)

    assert code == 0
    assert len(target.read_text().splitlines()) == 2
    assert "2 ejemplos exportados" in capsys.readouterr().out
