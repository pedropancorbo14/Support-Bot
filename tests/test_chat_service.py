from pathlib import Path

import pytest
from sqlalchemy import select

from app.chat.prompt import EndUser, SystemPrompt
from app.chat.service import (
    LLM_ERROR_REPLY,
    USER_REQUESTED_HUMAN,
    ChatService,
    Completed,
    ConversationStateError,
    Failed,
    Token,
)
from app.models import (
    ConversationKind,
    ConversationStatus,
    Message,
    MessageFlag,
    MessageRole,
    WebhookDelivery,
)
from app.rag.ingest import upsert_document
from app.webhooks import EVENT_ESCALATED, EVENT_MESSAGE_CREATED
from app.worker import embed_pending_chunks
from tests.fakes import FakeLlmClient

pytestmark = pytest.mark.anyio

USER = EndUser(user_id="u-42", name="Ana", plan="pro", locale="es-ES")


@pytest.fixture
def system_prompt(tmp_path: Path) -> SystemPrompt:
    path = tmp_path / "system.md"
    path.write_text("Eres el asistente de soporte de {app_name}.")
    return SystemPrompt(path)


def make_chat(session, llm, settings, system_prompt, **overrides) -> ChatService:
    return ChatService(session, llm, settings.model_copy(update=overrides), system_prompt)


@pytest.fixture
def chat(session, fake_llm, settings, system_prompt) -> ChatService:
    return make_chat(session, fake_llm, settings, system_prompt)


async def collect(events) -> list:
    return [event async for event in events]


async def deliveries(session) -> list[WebhookDelivery]:
    query = select(WebhookDelivery).order_by(WebhookDelivery.created_at)
    return list(await session.scalars(query.execution_options(populate_existing=True)))


async def test_start_conversation_reuses_open_one_and_updates_user(chat, demo_app) -> None:
    client_app, _ = demo_app

    first = await chat.start_conversation(client_app, USER)
    again = await chat.start_conversation(client_app, EndUser("u-42", "Ana B.", "max", "en"))
    other = await chat.start_conversation(client_app, EndUser("u-99"))

    assert first.kind == ConversationKind.BOT
    assert again.id == first.id
    assert (again.user_name, again.user_plan, again.user_locale) == ("Ana B.", "max", "en")
    assert other.id != first.id


async def test_reply_streams_and_records_metrics_and_context(
    session, sessionmaker, demo_app, settings, fake_llm, system_prompt
) -> None:
    client_app, _ = demo_app
    chat = make_chat(session, fake_llm, settings, system_prompt, rag_min_score=0.3)
    await upsert_document(
        session,
        client_app,
        external_id="faq",
        title="FAQ",
        content="## Pagos\n\nAceptamos tarjeta de crédito.",
        settings=settings,
    )
    await session.commit()
    await embed_pending_chunks(sessionmaker, fake_llm, settings)
    fake_llm.replies = ["Sí, aceptamos tarjeta."]
    conversation = await chat.start_conversation(client_app, USER)

    events = await collect(chat.reply(client_app, conversation, "aceptamos tarjeta de crédito"))

    tokens = [e.text for e in events if isinstance(e, Token)]
    assert "".join(tokens) == "Sí, aceptamos tarjeta."
    assert isinstance(events[-1], Completed) and events[-1].escalated is False

    prompt = fake_llm.calls[-1]
    assert prompt[0]["content"].startswith("Eres el asistente de soporte de Demo.")
    assert "[FAQ > Pagos] Aceptamos tarjeta de crédito." in prompt[0]["content"]
    assert "- Nombre: Ana" in prompt[0]["content"]
    assert prompt[-1] == {"role": "user", "content": "aceptamos tarjeta de crédito"}

    user_message, answer = await chat.messages(conversation)
    assert user_message.role == MessageRole.USER
    assert answer.role == MessageRole.ASSISTANT
    assert answer.content == "Sí, aceptamos tarjeta."
    assert answer.prompt_tokens > 0 and answer.completion_tokens == 3
    assert answer.latency_ms is not None and answer.ttft_ms is not None
    assert answer.ttft_ms <= answer.latency_ms
    [chunk] = answer.retrieved_chunks
    assert chunk["document"] == "FAQ" and chunk["section"] == "Pagos" and chunk["score"] > 0.3
    assert answer.flags == []


async def test_reply_without_documentation_injects_notice_and_flags(chat, demo_app, fake_llm):
    client_app, _ = demo_app
    conversation = await chat.start_conversation(client_app, USER)

    await collect(chat.reply(client_app, conversation, "¿Hola?"))

    assert "No hay documentación relevante para esta consulta" in fake_llm.calls[-1][0]["content"]
    [_, answer] = await chat.messages(conversation)
    assert answer.flags == [MessageFlag.NO_CONTEXT]


async def test_model_escalation_hides_tag_and_opens_human_conversation(
    session, settings, system_prompt, demo_app
) -> None:
    client_app, _ = demo_app
    llm = FakeLlmClient(
        ["Te paso con una persona. [ESCALAR: pide un reembolso]"], stream_chunk_size=3
    )
    chat = make_chat(session, llm, settings, system_prompt)
    conversation = await chat.start_conversation(client_app, USER)

    events = await collect(chat.reply(client_app, conversation, "Quiero mi dinero"))

    shown = "".join(e.text for e in events if isinstance(e, Token))
    assert "ESCALAR" not in shown and "[" not in shown
    done = events[-1]
    assert isinstance(done, Completed) and done.escalated and done.human_conversation_id

    [_, answer] = await chat.messages(conversation)
    assert answer.content == "Te paso con una persona."
    assert answer.raw_content.endswith("[ESCALAR: pide un reembolso]")
    assert MessageFlag.ESCALATED in answer.flags
    assert conversation.status == ConversationStatus.CLOSED
    assert (conversation.escalation_reason, conversation.escalated_by) == (
        "pide un reembolso",
        "model",
    )

    human = await chat.get_conversation(client_app.id, done.human_conversation_id)
    assert (human.kind, human.status, human.parent_id) == ("human", "open", conversation.id)
    assert human.user_name == "Ana"

    [delivery] = await deliveries(session)
    assert delivery.event == EVENT_ESCALATED
    assert delivery.payload == {
        "conversation_id": str(conversation.id),
        "human_conversation_id": str(human.id),
        "user": {"id": "u-42", "name": "Ana", "plan": "pro", "locale": "es-ES"},
        "reason": "pide un reembolso",
        "escalated_by": "model",
        "summary": None,
        "conversation_url": f"http://localhost:8000/admin/conversations/{conversation.id}",
        "human_conversation_url": f"http://localhost:8000/admin/conversations/{human.id}",
    }


async def test_llm_failure_returns_friendly_message_and_flags(
    session, settings, system_prompt, demo_app
) -> None:
    client_app, _ = demo_app
    chat = make_chat(session, FakeLlmClient(fail=True), settings, system_prompt)
    conversation = await chat.start_conversation(client_app, USER)

    events = await collect(chat.reply(client_app, conversation, "Hola"))

    assert events == [Failed(events[0].message_id, LLM_ERROR_REPLY)]
    [_, answer] = await chat.messages(conversation)
    assert answer.content == LLM_ERROR_REPLY
    assert answer.flags == [MessageFlag.LLM_ERROR]
    assert conversation.status == ConversationStatus.OPEN  # puede seguir o pedir una persona


async def test_long_history_is_summarized(session, settings, system_prompt, demo_app) -> None:
    client_app, _ = demo_app
    llm = FakeLlmClient(["R1", "R2", "RESUMEN-1", "R3"])
    chat = make_chat(session, llm, settings, system_prompt, history_max_messages=2)
    conversation = await chat.start_conversation(client_app, USER)

    for question in ("P1", "P2", "P3"):
        await collect(chat.reply(client_app, conversation, question))

    # Antes de P3 hay 4 mensajes: los 2 antiguos se resumen y los 2 recientes van literales.
    summary_call, last_call = llm.calls[-2], llm.calls[-1]
    assert summary_call[-1]["content"] == "Usuario: P1\nAsistente: R1"
    assert conversation.summary == "RESUMEN-1"
    assert "## Resumen de la conversación anterior\nRESUMEN-1" in last_call[0]["content"]
    assert [m["content"] for m in last_call[1:]] == ["P2", "R2", "P3"]


async def test_failed_replies_are_excluded_from_history(
    session, settings, system_prompt, demo_app
) -> None:
    client_app, _ = demo_app
    failing = make_chat(session, FakeLlmClient(fail=True), settings, system_prompt)
    conversation = await failing.start_conversation(client_app, USER)
    await collect(failing.reply(client_app, conversation, "P1"))
    llm = FakeLlmClient(["R2"])

    await collect(
        make_chat(session, llm, settings, system_prompt).reply(client_app, conversation, "P2")
    )

    assert [m["content"] for m in llm.calls[-1][1:]] == ["P1", "P2"]


async def test_user_requested_escalation(chat, session, demo_app) -> None:
    client_app, _ = demo_app
    conversation = await chat.start_conversation(client_app, USER)

    human = await chat.escalate(conversation)

    assert conversation.status == ConversationStatus.CLOSED
    assert conversation.escalated_by == "user"
    [delivery] = await deliveries(session)
    assert delivery.payload["reason"] == USER_REQUESTED_HUMAN
    assert delivery.payload["human_conversation_id"] == str(human.id)
    # Mientras siga abierta, el usuario vuelve a la conversación humana.
    assert (await chat.start_conversation(client_app, USER)).id == human.id


async def test_human_conversation_flow(chat, session, demo_app) -> None:
    client_app, _ = demo_app
    bot = await chat.start_conversation(client_app, USER)
    human = await chat.escalate(bot)

    user_message = await chat.post_user_message(human, "¿Hay alguien?")
    await chat.post_agent_message(human, "Hola Ana, soy Luis.", "Luis")
    await chat.close(human)

    messages = await chat.messages(human)
    assert [(m.role, m.content, m.author_name) for m in messages] == [
        ("user", "¿Hay alguien?", None),
        ("agent", "Hola Ana, soy Luis.", "Luis"),
    ]
    events = [d.event for d in await deliveries(session)]
    assert events == [EVENT_ESCALATED, EVENT_MESSAGE_CREATED]
    created = (await deliveries(session))[1].payload
    assert created["message"] == {"id": user_message.id, "content": "¿Hay alguien?"}
    assert created["user_id"] == "u-42"

    new = await chat.start_conversation(client_app, USER)
    assert new.kind == ConversationKind.BOT and new.id not in (bot.id, human.id)


async def test_state_rules(chat, demo_app) -> None:
    client_app, _ = demo_app
    bot = await chat.start_conversation(client_app, USER)
    human = await chat.escalate(bot)

    with pytest.raises(ConversationStateError):
        await collect(chat.reply(client_app, human, "hola"))  # humana: no responde el bot
    with pytest.raises(ConversationStateError):
        await collect(chat.reply(client_app, bot, "hola"))  # cerrada
    with pytest.raises(ConversationStateError):
        await chat.post_agent_message(bot, "x", None)
    with pytest.raises(ConversationStateError):
        await chat.escalate(bot)
    await chat.close(human)
    with pytest.raises(ConversationStateError):
        await chat.post_user_message(human, "x")


async def test_get_conversation_is_scoped_to_app(chat, demo_app, other_app) -> None:
    conversation = await chat.start_conversation(demo_app[0], USER)

    with pytest.raises(LookupError):
        await chat.get_conversation(other_app[0].id, conversation.id)


async def test_user_message_is_stored_even_if_stream_is_abandoned(chat, session, demo_app):
    client_app, _ = demo_app
    conversation = await chat.start_conversation(client_app, USER)

    events = chat.reply(client_app, conversation, "Hola")
    await anext(events)
    await events.aclose()

    stored = list(await session.scalars(select(Message.role)))
    assert stored == ["user"]
