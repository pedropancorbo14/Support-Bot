import asyncio
import json
import time
import uuid

import httpx
import pytest
from sqlalchemy import select

from app.chat.prompt import SystemPrompt
from app.chat.service import LLM_ERROR_REPLY, ChatService
from app.main import create_app
from app.models import ClientApp, Conversation, Message, WebhookDelivery
from tests.conftest import bearer, make_token
from tests.fakes import FakeLlmClient

pytestmark = pytest.mark.anyio

ORIGIN = "https://app.example.com"


def parse_sse(body: str) -> list[tuple[str, dict]]:
    events = []
    for block in body.strip().split("\n\n"):
        fields = dict(
            line.split(": ", 1) for line in block.splitlines() if not line.startswith(":")
        )
        if "event" in fields:
            events.append((fields["event"], json.loads(fields["data"])))
    return events


@pytest.fixture
async def widget_app(sessionmaker, demo_app) -> tuple[ClientApp, dict[str, str]]:
    client_app, _ = demo_app
    async with sessionmaker() as session:
        stored = await session.get(ClientApp, client_app.id)
        stored.allowed_origins = [ORIGIN]
        await session.commit()
    return stored, bearer(make_token(stored))


async def open_conversation(client, headers) -> dict:
    response = await client.post("/v1/conversations", headers=headers)
    assert response.status_code == 200
    return response.json()


async def test_open_conversation_returns_history_and_reuses_it(client, widget_app) -> None:
    _, headers = widget_app

    first = await open_conversation(client, headers)
    second = await open_conversation(client, headers)

    assert (first["kind"], first["status"], first["messages"]) == ("bot", "open", [])
    assert second["id"] == first["id"]


async def test_send_message_streams_sse(client, widget_app, fake_llm) -> None:
    _, headers = widget_app
    fake_llm.replies = ["Hola, ¿en qué te ayudo?"]
    conversation = await open_conversation(client, headers)

    response = await client.post(
        f"/v1/conversations/{conversation['id']}/messages",
        json={"content": "Hola"},
        headers=headers,
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(response.text)
    assert "".join(data["text"] for name, data in events if name == "token") == (
        "Hola, ¿en qué te ayudo?"
    )
    name, done = events[-1]
    assert name == "done"
    assert done["escalated"] is False and done["human_conversation_id"] is None

    history = await client.get(f"/v1/conversations/{conversation['id']}/messages", headers=headers)
    assert [(m["role"], m["content"]) for m in history.json()] == [
        ("user", "Hola"),
        ("assistant", "Hola, ¿en qué te ayudo?"),
    ]
    after = await client.get(
        f"/v1/conversations/{conversation['id']}/messages?after={history.json()[0]['id']}",
        headers=headers,
    )
    assert [m["role"] for m in after.json()] == ["assistant"]
    assert "raw_content" not in after.json()[0]  # el widget no ve datos internos


async def test_stream_reports_escalation(client, widget_app, fake_llm) -> None:
    _, headers = widget_app
    fake_llm.replies = ["Te paso con el equipo. [ESCALAR: reembolso]"]
    conversation = await open_conversation(client, headers)

    response = await client.post(
        f"/v1/conversations/{conversation['id']}/messages", json={"content": "x"}, headers=headers
    )

    events = parse_sse(response.text)
    assert "ESCALAR" not in response.text
    done = events[-1][1]
    assert done["escalated"] is True and done["human_conversation_id"]
    assert (await open_conversation(client, headers))["id"] == done["human_conversation_id"]


async def test_llm_failure_offers_human(client, widget_app, fake_llm) -> None:
    _, headers = widget_app
    fake_llm.fail = True
    conversation = await open_conversation(client, headers)

    response = await client.post(
        f"/v1/conversations/{conversation['id']}/messages", json={"content": "x"}, headers=headers
    )

    [(name, data)] = parse_sse(response.text)
    assert name == "error"
    assert data["message"] == LLM_ERROR_REPLY and data["offer_human"] is True


async def test_human_flow_with_realtime_events(
    client, widget_app, sessionmaker, settings, tmp_path
) -> None:
    client_app, headers = widget_app
    bot = await open_conversation(client, headers)

    escalation = await client.post(f"/v1/conversations/{bot['id']}/escalate", headers=headers)
    human_id = escalation.json()["human_conversation_id"]
    sent = await client.post(
        f"/v1/conversations/{human_id}/messages", json={"content": "¿Hola?"}, headers=headers
    )
    closed_bot = await client.post(
        f"/v1/conversations/{bot['id']}/messages", json={"content": "x"}, headers=headers
    )

    assert escalation.status_code == 201
    assert sent.status_code == 201 and sent.json()["role"] == "user"
    assert closed_bot.status_code == 409

    stream = asyncio.create_task(
        client.get(
            f"/v1/conversations/{human_id}/events?after={sent.json()['id']}", headers=headers
        )
    )
    await asyncio.sleep(0.2)  # el widget ya está escuchando
    async with sessionmaker() as session:
        chat = ChatService(session, FakeLlmClient(), settings, SystemPrompt(tmp_path / "s.md"))
        human = await chat.get_conversation(client_app.id, uuid.UUID(human_id))
        await chat.post_agent_message(human, "Hola Ana, soy Luis.", "Luis")
        await chat.close(human)
    response = await asyncio.wait_for(stream, timeout=5)

    events = parse_sse(response.text)
    assert [name for name, _ in events] == ["message", "closed"]
    message = events[0][1]
    assert (message["role"], message["content"], message["author_name"]) == (
        "agent",
        "Hola Ana, soy Luis.",
        "Luis",
    )
    assert f"id: {message['id']}" in response.text  # permite reanudar con Last-Event-ID

    async with sessionmaker() as session:
        events_sent = list(await session.scalars(select(WebhookDelivery.event)))
    assert sorted(events_sent) == ["conversation.escalated", "message.created"]
    reopened = await open_conversation(client, headers)
    assert reopened["kind"] == "bot" and reopened["id"] not in (bot["id"], human_id)


async def test_events_stream_ends_when_token_expires(client, widget_app) -> None:
    client_app, headers = widget_app
    bot = await open_conversation(client, headers)
    human_id = (
        await client.post(f"/v1/conversations/{bot['id']}/escalate", headers=headers)
    ).json()["human_conversation_id"]
    now = int(time.time())
    short = bearer(make_token(client_app, iat=now, exp=now + 1))

    started = time.monotonic()
    response = await client.get(f"/v1/conversations/{human_id}/events", headers=short)

    assert response.status_code == 200
    assert parse_sse(response.text) == []
    assert time.monotonic() - started < 5


async def test_events_only_for_human_conversations(client, widget_app) -> None:
    _, headers = widget_app
    bot = await open_conversation(client, headers)

    response = await client.get(f"/v1/conversations/{bot['id']}/events", headers=headers)

    assert response.status_code == 409


async def test_users_cannot_access_each_others_conversations(client, widget_app) -> None:
    client_app, headers = widget_app
    conversation = await open_conversation(client, headers)
    intruder = bearer(make_token(client_app, user_id="otro"))

    responses = [
        await client.get(f"/v1/conversations/{conversation['id']}/messages", headers=intruder),
        await client.post(
            f"/v1/conversations/{conversation['id']}/messages",
            json={"content": "x"},
            headers=intruder,
        ),
        await client.post(f"/v1/conversations/{conversation['id']}/escalate", headers=intruder),
    ]

    assert [r.status_code for r in responses] == [404, 404, 404]


async def test_feedback(client, widget_app, sessionmaker) -> None:
    client_app, headers = widget_app
    conversation = await open_conversation(client, headers)
    await client.post(
        f"/v1/conversations/{conversation['id']}/messages", json={"content": "x"}, headers=headers
    )
    user_message, answer = (
        await client.get(f"/v1/conversations/{conversation['id']}/messages", headers=headers)
    ).json()

    up = await client.post(
        f"/v1/messages/{answer['id']}/feedback", json={"rating": "up"}, headers=headers
    )
    down = await client.post(
        f"/v1/messages/{answer['id']}/feedback", json={"rating": "down"}, headers=headers
    )
    on_user = await client.post(
        f"/v1/messages/{user_message['id']}/feedback", json={"rating": "up"}, headers=headers
    )
    other_user = await client.post(
        f"/v1/messages/{answer['id']}/feedback",
        json={"rating": "up"},
        headers=bearer(make_token(client_app, user_id="otro")),
    )
    invalid = await client.post(
        f"/v1/messages/{answer['id']}/feedback", json={"rating": "meh"}, headers=headers
    )

    assert [r.status_code for r in (up, down, on_user, other_user, invalid)] == [
        204,
        204,
        404,
        404,
        422,
    ]
    async with sessionmaker() as session:
        assert await session.scalar(select(Message.rating).where(Message.id == answer["id"])) == -1


async def test_message_validation(client, widget_app) -> None:
    _, headers = widget_app
    conversation = await open_conversation(client, headers)

    empty = await client.post(
        f"/v1/conversations/{conversation['id']}/messages", json={"content": ""}, headers=headers
    )
    huge = await client.post(
        f"/v1/conversations/{conversation['id']}/messages",
        json={"content": "x" * 4001},
        headers=headers,
    )

    assert (empty.status_code, huge.status_code) == (422, 422)


# -- CORS --------------------------------------------------------------------------------


async def test_cors_preflight_allows_only_configured_origins(client, widget_app) -> None:
    preflight = {"Access-Control-Request-Method": "POST"}

    allowed = await client.options("/v1/conversations", headers={"Origin": ORIGIN, **preflight})
    denied = await client.options(
        "/v1/conversations", headers={"Origin": "https://malo.example.com", **preflight}
    )

    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == ORIGIN
    assert "authorization" in allowed.headers["access-control-allow-headers"].lower()
    assert denied.status_code == 400
    assert "access-control-allow-origin" not in denied.headers


async def test_origin_must_belong_to_the_tokens_app(
    client, widget_app, other_app, sessionmaker
) -> None:
    async with sessionmaker() as session:
        other = await session.get(ClientApp, other_app[0].id)
        other.allowed_origins = ["https://otra.example.com"]
        await session.commit()
    _, headers = widget_app

    same = await client.post("/v1/conversations", headers={"Origin": ORIGIN, **headers})
    foreign = await client.post(
        "/v1/conversations", headers={"Origin": "https://otra.example.com", **headers}
    )

    assert same.status_code == 200
    assert same.headers["access-control-allow-origin"] == ORIGIN
    assert foreign.status_code == 403


async def test_admin_endpoints_have_no_cors(client, widget_app) -> None:
    response = await client.options(
        "/admin/conversations",
        headers={"Origin": ORIGIN, "Access-Control-Request-Method": "GET"},
    )

    assert "access-control-allow-origin" not in response.headers


# -- Rate limit --------------------------------------------------------------------------


async def test_rate_limit_per_user(widget_app, settings) -> None:
    client_app, headers = widget_app
    app = create_app(
        settings.model_copy(update={"rate_limit_user_per_minute": 2}), llm=FakeLlmClient()
    )
    async with app.router.lifespan_context(app):
        await app.state.redis.flushdb()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            codes = [(await http.post("/v1/conversations", headers=headers)) for _ in range(3)]
            other_user = await http.post(
                "/v1/conversations", headers=bearer(make_token(client_app, user_id="otro"))
            )

    assert [r.status_code for r in codes] == [200, 200, 429]
    assert 0 < int(codes[-1].headers["retry-after"]) <= 60
    assert other_user.status_code == 200


async def test_rate_limit_per_ip(widget_app, settings) -> None:
    client_app, _ = widget_app
    app = create_app(
        settings.model_copy(update={"rate_limit_ip_per_minute": 2}), llm=FakeLlmClient()
    )
    async with app.router.lifespan_context(app):
        await app.state.redis.flushdb()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            codes = [
                (
                    await http.post(
                        "/v1/conversations",
                        headers=bearer(make_token(client_app, user_id=f"u{i}")),
                    )
                ).status_code
                for i in range(3)
            ]

    assert codes == [200, 200, 429]


async def test_widget_js_is_served(client) -> None:
    response = await client.get("/widget.js")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/javascript")


async def test_conversation_ids_are_not_guessable(client, widget_app, sessionmaker) -> None:
    _, headers = widget_app
    conversation = await open_conversation(client, headers)

    async with sessionmaker() as session:
        stored = await session.get(Conversation, uuid.UUID(conversation["id"]))
    assert stored.id.version == 4
