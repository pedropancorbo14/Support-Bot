# Webhooks

El servicio avisa a cada app cliente con peticiones `POST` firmadas a su URL de webhook.

## Configuración

```bash
python -m app.cli create-app --slug mi-app --name "Mi App" --webhook-url https://mi-app.com/support/webhook
python -m app.cli update-app --slug mi-app --webhook-url https://mi-app.com/support/webhook
python -m app.cli update-app --slug mi-app --webhook-url ""      # desactivar
```

`create-app` genera el **secreto del webhook** (`whsec_…`) y lo muestra una sola vez,
junto a la API key.

## Formato

```http
POST /support/webhook
Content-Type: application/json
X-Support-Event: conversation.escalated
X-Support-Delivery: 5b1c…            # id único del envío; úsalo para deduplicar
X-Support-Signature: t=1762257600,v1=8f3a…

{"id": "5b1c…", "event": "conversation.escalated", "created_at": "2026-11-04T12:00:00+00:00", "data": {…}}
```

Un mismo envío puede llegar más de una vez, por ejemplo si tu servidor procesa la
petición pero responde tarde. Deduplica por `id`.

## Eventos

### `conversation.escalated`

La conversación del bot se ha cerrado y hay una conversación humana esperando a un agente.

```json
{
  "conversation_id": "…",
  "human_conversation_id": "…",
  "user": {"id": "42", "name": "Ana", "plan": "pro", "locale": "es-ES"},
  "reason": "pide un reembolso fuera de plazo",
  "escalated_by": "model",
  "summary": "La usuaria pide el reembolso de un pedido de hace 40 días…",
  "conversation_url": "https://soporte.midominio.com/admin/conversations/…",
  "human_conversation_url": "https://soporte.midominio.com/admin/conversations/…"
}
```

- `escalated_by`: `model` si lo decidió el bot con `[ESCALAR: …]`, o `user` si lo pidió
  el usuario.
- `summary` lo genera el worker con el LLM **antes del primer envío**, así que el
  usuario no espera. Si el LLM falla, se envían los últimos mensajes literales
  (`Últimos mensajes:\nUsuario: …`).
- Las URLs apuntan a la API administrativa y requieren `X-Api-Key`.

### `message.created`

El usuario ha escrito en una conversación humana.

```json
{
  "conversation_id": "…",
  "user_id": "42",
  "message": {"id": 17, "content": "¿Sigue ahí alguien?"},
  "conversation_url": "https://soporte.midominio.com/admin/conversations/…"
}
```

Los agentes responden con `POST /admin/conversations/{id}/messages` y cierran con
`POST /admin/conversations/{id}/close` (ver [API administrativa](api-admin.md)).

## Verificar la firma

`X-Support-Signature` tiene el formato `t=<unix>,v1=<hex>`, donde
`hex = HMAC-SHA256(secreto, "<unix>." + cuerpo_en_bruto)`.

1. Lee el cuerpo **en bruto**, antes de decodificar el JSON.
2. Recalcula el HMAC y compáralo con `v1` en tiempo constante.
3. Rechaza los mensajes con `t` de más de 5 minutos de antigüedad (anti-replay).

El código para Laravel llegará con la guía de integración de la fase 5.

## Entrega y reintentos

- Los eventos se guardan en `webhook_deliveries` en la **misma transacción** que el
  escalado o el mensaje: no se pierden aunque falle la red o caiga el worker.
- El worker los envía (primero los webhooks, después los embeddings). Cualquier 2xx
  cuenta como entregado.
- Ante un error HTTP o de red, se reintenta con backoff exponencial:
  `WEBHOOK_RETRY_BASE_SECONDS × 2^(intento−1)` (30 s, 1 min, 2 min…), con un máximo de
  1 h entre intentos. Tras `WEBHOOK_MAX_ATTEMPTS` intentos (8) queda `failed`.
- Si la app no tiene URL de webhook, el envío queda `failed` con ese motivo.
- Cada envío registra `attempts`, `last_status_code`, `last_error`, `next_attempt_at` y
  `delivered_at`.

```sql
SELECT event, status, attempts, last_status_code, last_error
FROM webhook_deliveries ORDER BY created_at DESC LIMIT 20;
```

## Tests

`tests/test_webhooks.py`: firma, cabeceras y cuerpo, backoff hasta `failed`, error de
transporte seguido de éxito, app sin webhook, y resumen con LLM y su respaldo.
