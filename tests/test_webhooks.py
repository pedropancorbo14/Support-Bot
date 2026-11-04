import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from app.models import (
    ClientApp,
    Conversation,
    DeliveryStatus,
    Message,
    WebhookDelivery,
)
from app.webhooks import (
    DELIVERY_HEADER,
    EVENT_ESCALATED,
    EVENT_HEADER,
    SIGNATURE_HEADER,
    deliver_pending,
    enqueue,
    sign,
    signature_header,
)
from tests.fakes import FakeLlmClient

pytestmark = pytest.mark.anyio

SECRET = "whsec_test"
# Referencia futura: los envíos recién creados ya están vencidos para el worker.
NOW = datetime.now(UTC).replace(microsecond=0) + timedelta(hours=1)


def test_signature_is_hmac_sha256_of_timestamp_and_body() -> None:
    body = b'{"a":1}'

    expected = hmac.new(SECRET.encode(), b"1700000000." + body, hashlib.sha256).hexdigest()

    assert sign(SECRET, 1700000000, body) == expected
    assert signature_header(SECRET, 1700000000, body) == f"t=1700000000,v1={expected}"


class Receiver:
    """Endpoint falso de la app cliente: guarda las peticiones y responde lo indicado."""

    def __init__(self, *responses: int | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        outcome = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(outcome, Exception):
            raise outcome
        return httpx.Response(outcome, text="respuesta")


@pytest.fixture
async def hooked_app(sessionmaker, demo_app) -> ClientApp:
    client_app, _ = demo_app
    async with sessionmaker() as session:
        stored = await session.get(ClientApp, client_app.id)
        stored.webhook_url = "https://app.example.com/webhooks/soporte"
        stored.webhook_secret = SECRET
        await session.commit()
    return stored


async def add_delivery(sessionmaker, app_id: int, event: str = "test.event", data=None) -> None:
    async with sessionmaker() as session:
        enqueue(session, app_id, event, data or {"hello": "mundo"})
        await session.commit()


async def deliver(sessionmaker, receiver: Receiver, settings, llm=None, now=NOW) -> int:
    async with httpx.AsyncClient(transport=httpx.MockTransport(receiver)) as http:
        return await deliver_pending(sessionmaker, http, llm or FakeLlmClient(), settings, now=now)


async def stored_delivery(sessionmaker) -> WebhookDelivery:
    async with sessionmaker() as session:
        return await session.scalar(select(WebhookDelivery))


async def test_successful_delivery_is_signed_and_recorded(sessionmaker, hooked_app, settings):
    await add_delivery(sessionmaker, hooked_app.id)
    receiver = Receiver(200)

    assert await deliver(sessionmaker, receiver, settings) == 1

    [request] = receiver.requests
    delivery = await stored_delivery(sessionmaker)
    assert str(request.url) == "https://app.example.com/webhooks/soporte"
    assert request.headers[EVENT_HEADER] == "test.event"
    assert request.headers[DELIVERY_HEADER] == str(delivery.id)
    timestamp = int(NOW.timestamp())
    assert request.headers[SIGNATURE_HEADER] == signature_header(SECRET, timestamp, request.content)
    body = json.loads(request.content)
    assert body["id"] == str(delivery.id)
    assert body["event"] == "test.event"
    assert body["data"] == {"hello": "mundo"}
    assert (delivery.status, delivery.attempts, delivery.last_status_code) == (
        DeliveryStatus.DELIVERED,
        1,
        200,
    )
    assert delivery.delivered_at == NOW
    assert await deliver(sessionmaker, receiver, settings) == 0  # nada más pendiente


async def test_failures_retry_with_exponential_backoff_until_max_attempts(
    sessionmaker, hooked_app, settings
) -> None:
    settings = settings.model_copy(update={"webhook_max_attempts": 3})
    await add_delivery(sessionmaker, hooked_app.id)
    receiver = Receiver(500)

    await deliver(sessionmaker, receiver, settings)
    first = await stored_delivery(sessionmaker)
    assert (first.status, first.attempts, first.last_status_code) == ("pending", 1, 500)
    assert first.last_error == "HTTP 500: respuesta"
    assert first.next_attempt_at == NOW + timedelta(seconds=30)

    assert await deliver(sessionmaker, receiver, settings) == 0  # aún no toca

    await deliver(sessionmaker, receiver, settings, now=NOW + timedelta(seconds=30))
    second = await stored_delivery(sessionmaker)
    assert second.attempts == 2
    assert second.next_attempt_at == NOW + timedelta(seconds=30 + 60)

    await deliver(sessionmaker, receiver, settings, now=NOW + timedelta(seconds=90))
    final = await stored_delivery(sessionmaker)
    assert (final.status, final.attempts) == (DeliveryStatus.FAILED, 3)
    assert len(receiver.requests) == 3


async def test_transport_error_then_success(sessionmaker, hooked_app, settings) -> None:
    await add_delivery(sessionmaker, hooked_app.id)
    receiver = Receiver(httpx.ConnectError("caído"), 204)

    await deliver(sessionmaker, receiver, settings)
    failed = await stored_delivery(sessionmaker)
    assert failed.last_status_code is None
    assert "ConnectError" in failed.last_error

    await deliver(sessionmaker, receiver, settings, now=NOW + timedelta(minutes=1))
    delivered = await stored_delivery(sessionmaker)
    assert delivered.status == DeliveryStatus.DELIVERED
    assert delivered.last_error is None


async def test_app_without_webhook_marks_failed(sessionmaker, demo_app, settings) -> None:
    await add_delivery(sessionmaker, demo_app[0].id)
    receiver = Receiver(200)

    await deliver(sessionmaker, receiver, settings)

    delivery = await stored_delivery(sessionmaker)
    assert delivery.status == DeliveryStatus.FAILED
    assert delivery.last_error == "La app no tiene webhook configurado"
    assert receiver.requests == []


async def escalated_conversation(sessionmaker, app_id: int) -> uuid.UUID:
    async with sessionmaker() as session:
        conversation = Conversation(
            id=uuid.uuid4(), app_id=app_id, kind="bot", status="closed", external_user_id="u1"
        )
        session.add(conversation)
        await session.flush()
        session.add_all(
            [
                Message(conversation_id=conversation.id, role="user", content="No puedo pagar"),
                Message(conversation_id=conversation.id, role="assistant", content="Te paso."),
            ]
        )
        await session.commit()
        return conversation.id


async def test_escalation_summary_is_generated_before_sending(
    sessionmaker, hooked_app, settings
) -> None:
    conversation_id = await escalated_conversation(sessionmaker, hooked_app.id)
    await add_delivery(
        sessionmaker,
        hooked_app.id,
        EVENT_ESCALATED,
        {"conversation_id": str(conversation_id), "summary": None},
    )
    llm = FakeLlmClient(["El usuario no puede pagar."])
    receiver = Receiver(200)

    await deliver(sessionmaker, receiver, settings, llm=llm)

    assert json.loads(receiver.requests[0].content)["data"]["summary"] == (
        "El usuario no puede pagar."
    )
    assert llm.calls[0][-1]["content"] == "Usuario: No puedo pagar\nAsistente: Te paso."
    stored = await stored_delivery(sessionmaker)
    assert stored.payload["summary"] == "El usuario no puede pagar."  # no se regenera


async def test_escalation_summary_falls_back_to_transcript(
    sessionmaker, hooked_app, settings
) -> None:
    conversation_id = await escalated_conversation(sessionmaker, hooked_app.id)
    await add_delivery(
        sessionmaker,
        hooked_app.id,
        EVENT_ESCALATED,
        {"conversation_id": str(conversation_id), "summary": None},
    )
    receiver = Receiver(200)

    await deliver(sessionmaker, receiver, settings, llm=FakeLlmClient(fail=True))

    summary = json.loads(receiver.requests[0].content)["data"]["summary"]
    assert summary == "Últimos mensajes:\nUsuario: No puedo pagar\nAsistente: Te paso."
