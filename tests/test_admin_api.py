import httpx
import pytest

from app.chat.prompt import EndUser, SystemPrompt
from app.chat.service import ChatService
from app.models import Message
from tests.fakes import FakeLlmClient

pytestmark = pytest.mark.anyio

DOC = {"title": "FAQ", "content": "## Pagos\n\nAceptamos tarjeta."}


def auth(api_key: str) -> dict[str, str]:
    return {"X-Api-Key": api_key}


@pytest.mark.parametrize("headers", [{}, {"X-Api-Key": "sk_inventada"}])
async def test_requires_valid_api_key(client: httpx.AsyncClient, demo_app, headers) -> None:
    response = await client.put("/admin/documents/faq", json=DOC, headers=headers)

    assert response.status_code == 401


async def test_put_creates_then_is_idempotent(client: httpx.AsyncClient, demo_app) -> None:
    _, api_key = demo_app

    created = await client.put("/admin/documents/faq", json=DOC, headers=auth(api_key))
    repeated = await client.put("/admin/documents/faq", json=DOC, headers=auth(api_key))

    assert created.status_code == 200
    assert created.json() == {
        "external_id": "faq",
        "status": "created",
        "chunks": 1,
        "pending_embeddings": 1,
    }
    assert repeated.json()["status"] == "unchanged"


async def test_put_validates_body(client: httpx.AsyncClient, demo_app) -> None:
    response = await client.put(
        "/admin/documents/faq", json={"title": "", "content": "x"}, headers=auth(demo_app[1])
    )

    assert response.status_code == 422


async def test_delete(client: httpx.AsyncClient, demo_app) -> None:
    _, api_key = demo_app
    await client.put("/admin/documents/faq", json=DOC, headers=auth(api_key))

    first = await client.delete("/admin/documents/faq", headers=auth(api_key))
    second = await client.delete("/admin/documents/faq", headers=auth(api_key))

    assert (first.status_code, second.status_code) == (204, 404)


async def test_apps_cannot_touch_each_others_documents(
    client: httpx.AsyncClient, demo_app, other_app
) -> None:
    await client.put("/admin/documents/faq", json=DOC, headers=auth(demo_app[1]))

    response = await client.delete("/admin/documents/faq", headers=auth(other_app[1]))

    assert response.status_code == 404


@pytest.fixture
async def conversations(session, demo_app, settings, tmp_path):
    chat = ChatService(session, FakeLlmClient(), settings, SystemPrompt(tmp_path / "s.md"))
    bot = await chat.start_conversation(demo_app[0], EndUser("u-1", "Ana"))
    session.add(Message(conversation_id=bot.id, role="user", content="Hola"))
    await session.commit()
    human = await chat.escalate(bot, "quiere una persona")
    return bot, human


async def test_get_conversation_with_messages(client, demo_app, conversations) -> None:
    bot, human = conversations

    response = await client.get(f"/admin/conversations/{bot.id}", headers=auth(demo_app[1]))

    assert response.status_code == 200
    body = response.json()
    assert (body["kind"], body["status"], body["escalation_reason"]) == (
        "bot",
        "closed",
        "quiere una persona",
    )
    assert [(m["role"], m["content"]) for m in body["messages"]] == [("user", "Hola")]
    human_body = (
        await client.get(f"/admin/conversations/{human.id}", headers=auth(demo_app[1]))
    ).json()
    assert human_body["parent_id"] == str(bot.id)


async def test_conversations_are_scoped_to_app(client, other_app, conversations) -> None:
    bot, _ = conversations

    response = await client.get(f"/admin/conversations/{bot.id}", headers=auth(other_app[1]))
    listing = await client.get("/admin/conversations", headers=auth(other_app[1]))

    assert response.status_code == 404
    assert listing.json() == []


async def test_list_conversations_with_filters(client, demo_app, conversations) -> None:
    bot, human = conversations
    headers = auth(demo_app[1])

    everything = (await client.get("/admin/conversations", headers=headers)).json()
    humans = (await client.get("/admin/conversations?kind=human", headers=headers)).json()
    open_ = (await client.get("/admin/conversations?status=open", headers=headers)).json()
    nobody = (await client.get("/admin/conversations?user_id=nadie", headers=headers)).json()

    assert {c["id"] for c in everything} == {str(bot.id), str(human.id)}
    assert [c["id"] for c in humans] == [str(human.id)]
    assert [c["id"] for c in open_] == [str(human.id)]
    assert nobody == []


async def test_agent_replies_and_closes(client, demo_app, conversations) -> None:
    bot, human = conversations
    headers = auth(demo_app[1])

    reply = await client.post(
        f"/admin/conversations/{human.id}/messages",
        json={"content": "Hola Ana", "author_name": "Luis"},
        headers=headers,
    )
    on_bot = await client.post(
        f"/admin/conversations/{bot.id}/messages", json={"content": "x"}, headers=headers
    )
    closed = await client.post(f"/admin/conversations/{human.id}/close", headers=headers)
    closed_again = await client.post(f"/admin/conversations/{human.id}/close", headers=headers)

    assert reply.status_code == 201
    assert (reply.json()["role"], reply.json()["author_name"]) == ("agent", "Luis")
    assert on_bot.status_code == 409
    assert closed.status_code == 204
    assert closed_again.status_code == 409


async def test_unknown_conversation(client, demo_app) -> None:
    response = await client.get(
        "/admin/conversations/00000000-0000-0000-0000-000000000000", headers=auth(demo_app[1])
    )

    assert response.status_code == 404
