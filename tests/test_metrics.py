import pytest

from app.chat.prompt import EndUser, SystemPrompt
from app.chat.service import ChatService
from app.rag.ingest import upsert_document
from app.worker import embed_pending_chunks
from tests.fakes import FakeLlmClient

pytestmark = pytest.mark.anyio


async def collect(events) -> list:
    return [e async for e in events]


@pytest.fixture
async def activity(session, sessionmaker, demo_app, other_app, settings, tmp_path):
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
    prompt = SystemPrompt(tmp_path / "s.md")
    settings = settings.model_copy(update={"rag_min_score": 0.3})
    llm = FakeLlmClient(["Aceptamos tarjeta.", "No lo sé.", "Te paso. [ESCALAR: enfadado]"])
    chat = ChatService(session, llm, settings, prompt)

    ana = await chat.start_conversation(client_app, EndUser("ana"))
    await collect(chat.reply(client_app, ana, "aceptamos tarjeta de crédito"))
    await collect(chat.reply(client_app, ana, "¿horario?"))
    answers = [m for m in await chat.messages(ana) if m.role == "assistant"]
    await chat.rate(client_app.id, "ana", answers[0].id, 1)
    await chat.rate(client_app.id, "ana", answers[1].id, -1)

    luis = await chat.start_conversation(client_app, EndUser("luis"))
    await collect(chat.reply(client_app, luis, "quiero quejarme"))

    pepe = await chat.start_conversation(client_app, EndUser("pepe"))
    await chat.escalate(pepe)

    failing = ChatService(session, FakeLlmClient(fail=True), settings, prompt)
    eva = await failing.start_conversation(client_app, EndUser("eva"))
    await collect(failing.reply(client_app, eva, "hola"))

    # Actividad de otra app: no debe contar.
    other = ChatService(session, FakeLlmClient(), settings, prompt)
    await collect(
        other.reply(other_app[0], await other.start_conversation(other_app[0], EndUser("x")), "x")
    )


async def test_metrics_aggregate_the_apps_activity(client, demo_app, activity) -> None:
    response = await client.get("/admin/metrics", headers={"X-Api-Key": demo_app[1]})

    assert response.status_code == 200
    body = response.json()
    assert body["conversations"] == {
        "bot": 4,
        "human": 2,
        "escalated_by_model": 1,
        "escalated_by_user": 1,
        "escalation_rate": 0.5,
    }
    assert body["messages"] == {"user": 4, "assistant": 4, "agent": 0}
    answers = body["answers"]
    assert answers["total"] == 4
    # La respuesta fallida solo lleva llm_error (el fallo ocurre antes de recuperar).
    assert (answers["no_context"], answers["llm_errors"], answers["escalated"]) == (2, 1, 1)
    assert answers["llm_error_rate"] == 0.25
    assert answers["latency_ms"]["p95"] is not None and answers["ttft_ms"]["avg"] is not None
    assert answers["tokens"]["completion"] > 0
    assert body["ratings"] == {"up": 1, "down": 1, "satisfaction": 0.5, "rated_rate": 0.5}
    assert body["webhooks"] == {"pending": 2, "delivered": 0, "failed": 0}


async def test_metrics_time_window(client, demo_app, activity) -> None:
    headers = {"X-Api-Key": demo_app[1]}

    empty = await client.get(
        "/admin/metrics?since=2020-01-01T00:00:00Z&until=2020-02-01T00:00:00Z", headers=headers
    )
    invalid = await client.get(
        "/admin/metrics?since=2020-02-01T00:00:00Z&until=2020-01-01T00:00:00Z", headers=headers
    )

    assert empty.json()["answers"]["total"] == 0
    assert empty.json()["ratings"]["satisfaction"] is None
    assert invalid.status_code == 422


async def test_metrics_require_api_key(client) -> None:
    assert (await client.get("/admin/metrics")).status_code == 401
