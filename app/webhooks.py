"""Webhooks firmados hacia las apps cliente.

Los eventos se guardan en `webhook_deliveries` en la misma transacción que el cambio que
los origina (outbox). El worker los envía y reintenta con backoff exponencial. Cada
envío queda registrado con sus intentos, el último código HTTP y el último error.

Firma: `X-Support-Signature: t=<unix>,v1=<hex>`, donde `hex` es el HMAC-SHA256 con el
secreto del webhook de `"<unix>.<cuerpo>"`. El receptor debe recalcularlo sobre el cuerpo
en bruto y rechazar marcas de tiempo antiguas (por ejemplo, de más de 5 minutos).
"""

import hashlib
import hmac
import json
import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.chat.prompt import summarize, transcript
from app.config import Settings
from app.llm import LlmClient, LlmError
from app.models import ClientApp, DeliveryStatus, Message, WebhookDelivery

logger = logging.getLogger(__name__)

EVENT_ESCALATED = "conversation.escalated"
EVENT_MESSAGE_CREATED = "message.created"

SIGNATURE_HEADER = "X-Support-Signature"
EVENT_HEADER = "X-Support-Event"
DELIVERY_HEADER = "X-Support-Delivery"

SECRET_PREFIX = "whsec_"
MAX_RETRY_DELAY = timedelta(hours=1)
FALLBACK_SUMMARY_MESSAGES = 6


def generate_webhook_secret() -> str:
    return SECRET_PREFIX + secrets.token_urlsafe(32)


def sign(secret: str, timestamp: int, body: bytes) -> str:
    message = str(timestamp).encode() + b"." + body
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def signature_header(secret: str, timestamp: int, body: bytes) -> str:
    return f"t={timestamp},v1={sign(secret, timestamp, body)}"


def enqueue(session: AsyncSession, app_id: int, event: str, data: dict[str, Any]) -> None:
    session.add(WebhookDelivery(app_id=app_id, event=event, payload=data))


async def deliver_pending(
    sessionmaker: async_sessionmaker[AsyncSession],
    http: httpx.AsyncClient,
    llm: LlmClient,
    settings: Settings,
    *,
    batch_size: int = 10,
    now: datetime | None = None,
) -> int:
    """Envía hasta `batch_size` webhooks vencidos, cada uno en su propia transacción."""
    processed = 0
    while processed < batch_size:
        async with sessionmaker() as session, session.begin():
            current = now or datetime.now(UTC)
            row = (
                await session.execute(
                    select(WebhookDelivery, ClientApp)
                    .join(ClientApp, ClientApp.id == WebhookDelivery.app_id)
                    .where(
                        WebhookDelivery.status == DeliveryStatus.PENDING,
                        WebhookDelivery.next_attempt_at <= current,
                    )
                    .order_by(WebhookDelivery.next_attempt_at)
                    .limit(1)
                    .with_for_update(of=WebhookDelivery, skip_locked=True)
                )
            ).first()
            if row is None:
                break
            delivery, client_app = row
            await _complete_payload(session, llm, delivery)
            await _attempt(http, delivery, client_app, settings, current)
        processed += 1
    return processed


async def _complete_payload(
    session: AsyncSession, llm: LlmClient, delivery: WebhookDelivery
) -> None:
    """El resumen del escalado se genera aquí, no en la petición del usuario."""
    if delivery.event != EVENT_ESCALATED or delivery.payload.get("summary") is not None:
        return
    messages = list(
        await session.scalars(
            select(Message)
            .where(Message.conversation_id == uuid.UUID(delivery.payload["conversation_id"]))
            .order_by(Message.id)
        )
    )
    pairs = [(m.role, m.content) for m in messages]
    try:
        summary = await summarize(llm, transcript(pairs))
    except LlmError as exc:
        logger.warning("No se pudo resumir la conversación (%s); se envía un extracto", exc)
        summary = "Últimos mensajes:\n" + transcript(pairs[-FALLBACK_SUMMARY_MESSAGES:])
    delivery.payload = {**delivery.payload, "summary": summary}


async def _attempt(
    http: httpx.AsyncClient,
    delivery: WebhookDelivery,
    client_app: ClientApp,
    settings: Settings,
    now: datetime,
) -> None:
    delivery.attempts += 1
    if not client_app.webhook_url or not client_app.webhook_secret:
        delivery.status = DeliveryStatus.FAILED
        delivery.last_error = "La app no tiene webhook configurado"
        return

    body = json.dumps(
        {
            "id": str(delivery.id),
            "event": delivery.event,
            "created_at": delivery.created_at.isoformat(),
            "data": delivery.payload,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    headers = {
        "Content-Type": "application/json",
        SIGNATURE_HEADER: signature_header(client_app.webhook_secret, int(now.timestamp()), body),
        EVENT_HEADER: delivery.event,
        DELIVERY_HEADER: str(delivery.id),
    }
    try:
        response = await http.post(
            client_app.webhook_url, content=body, headers=headers, timeout=settings.webhook_timeout
        )
    except httpx.HTTPError as exc:
        delivery.last_status_code = None
        delivery.last_error = f"Error de transporte: {exc!r}"
    else:
        delivery.last_status_code = response.status_code
        if response.is_success:
            delivery.status = DeliveryStatus.DELIVERED
            delivery.delivered_at = now
            delivery.last_error = None
            return
        delivery.last_error = f"HTTP {response.status_code}: {response.text[:500]}"

    if delivery.attempts >= settings.webhook_max_attempts:
        delivery.status = DeliveryStatus.FAILED
        logger.error("Webhook %s descartado tras %d intentos", delivery.id, delivery.attempts)
    else:
        delay = timedelta(
            seconds=settings.webhook_retry_base_seconds * 2 ** (delivery.attempts - 1)
        )
        delivery.next_attempt_at = now + min(delay, MAX_RETRY_DELAY)
