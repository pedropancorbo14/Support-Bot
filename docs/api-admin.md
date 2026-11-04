# API administrativa

Endpoints para que cada app cliente gestione sus datos. Todos exigen la cabecera
`X-Api-Key` con la clave de la app (la muestra `create-app`). Sin clave, o con una
clave inválida, responden `401`. Cada app solo ve y modifica sus propios datos.

La documentación OpenAPI interactiva está en `/docs` (Swagger) y en `/openapi.json`.

## Alta de una app

No hay endpoint: las apps se dan de alta desde el CLI.

```bash
python -m app.cli create-app --slug mi-app --name "Mi App"
```

Se guarda el SHA-256 de la clave, nunca la clave en claro. Si la pierdes, hay que
generar una nueva. En el mismo momento se muestran también el **secreto JWT**, con el
que tu app firma los tokens de sus usuarios (ver [API del widget](api-widget.md)), y el
**secreto del webhook** (ver [webhooks](webhooks.md)).

| Opción | Descripción |
|---|---|
| `--webhook-url URL` | Destino de los webhooks. |
| `--allowed-origin ORIGEN` | Origen desde el que se carga el widget (repetible). |

Para cambiar una app existente usa `update-app --slug mi-app` con `--webhook-url`,
`--allowed-origin` (sustituye la lista), `--rotate-jwt-secret` o `--rotate-webhook-secret`.

## `PUT /admin/documents/{external_id}`

Crea o actualiza un documento. Es idempotente: reenviar el mismo contenido no hace nada.
`external_id` es el identificador del documento en tu app (de 1 a 255 caracteres).

```http
PUT /admin/documents/faq-pagos
X-Api-Key: sk_...
Content-Type: application/json

{"title": "Preguntas sobre pagos", "content": "## Tarjeta\n\nAceptamos Visa..."}
```

Respuesta `200`:

```json
{"external_id": "faq-pagos", "status": "created", "chunks": 3, "pending_embeddings": 3}
```

| Campo | Significado |
|---|---|
| `status` | `created`, `updated` o `unchanged`. |
| `chunks` | Número de chunks del documento. |
| `pending_embeddings` | Chunks que el worker aún no ha embebido. Hasta entonces no se recuperan. |

Errores: `401` sin clave válida; `422` si `title` o `content` están vacíos.

## `DELETE /admin/documents/{external_id}`

Borra el documento y todos sus chunks. Responde `204`, o `404` si el documento no
existe en tu app.

```bash
curl -X DELETE https://soporte.midominio.com/admin/documents/faq-pagos -H "X-Api-Key: sk_..."
```


## `GET /admin/conversations`

Conversaciones de la app, de la más reciente a la más antigua.

| Parámetro | Descripción |
|---|---|
| `user_id` | Solo las de ese usuario (id externo del JWT). |
| `kind` | `bot` o `human`. |
| `status` | `open` o `closed`. |
| `limit` / `offset` | Paginación (`limit` de 1 a 200; 50 por defecto). |

Para ver la cola de conversaciones pendientes de un agente:
`GET /admin/conversations?kind=human&status=open`.

## `GET /admin/conversations/{id}`

Conversación completa: datos del usuario, estado, motivo del escalado, `parent_id` (la
conversación del bot de la que procede, si es humana) y todos los mensajes con sus
métricas (`prompt_tokens`, `completion_tokens`, `latency_ms`, `ttft_ms`,
`retrieved_chunks`, `flags`, `raw_content`). Devuelve `404` si no existe o es de otra app.

## `POST /admin/conversations/{id}/messages`

Respuesta de un agente humano. Solo es válida en conversaciones humanas abiertas: en
cualquier otro caso devuelve `409`.

```json
{"content": "Hola Ana, soy Luis. Ya veo el cobro duplicado.", "author_name": "Luis"}
```

Respuesta `201` con el mensaje creado (`role: "agent"`).

## `POST /admin/conversations/{id}/close`

El agente cierra la conversación humana. Devuelve `204`, o `409` si no es humana o ya
estaba cerrada. Cuando el usuario vuelva al widget, empezará una conversación nueva con
el bot.

## `GET /admin/metrics`

Métricas agregadas de la app: conversaciones, escalados, latencias (media, p50 y p95),
tiempo hasta el primer token, tokens, falta de documentación, errores del LLM,
valoraciones y estado de los webhooks. Por defecto cubre los últimos 30 días; se puede
acotar con `?since=` y `?until=` (ISO 8601). Detalle de cada campo en
[observabilidad](observabilidad.md).
