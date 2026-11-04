# API del widget (`/v1`)

Es la API que usa `widget.js` desde el navegador del usuario final. La referencia
interactiva está en `/docs` (Swagger) y en `/openapi.json`.

## Autenticación: JWT del usuario final

El servicio no gestiona usuarios ni contraseñas. Tu app emite un **JWT HS256 de corta
duración**, firmado con el **secreto JWT de la app** (lo muestra `create-app`), y el
widget lo envía en `Authorization: Bearer <token>`.

| Claim | Obligatorio | Descripción |
|---|---|---|
| `iss` | sí | **Slug de la app** (`--slug` de `create-app`). Indica qué secreto usar. |
| `user_id` | sí | Id del usuario en tu app (texto o número). |
| `iat` | sí | Momento de emisión (Unix). |
| `exp` | sí | Caducidad (Unix). `exp − iat` no puede superar `JWT_MAX_TTL_SECONDS` (15 min). |
| `name` | no | Nombre que verá el bot (y el agente). |
| `plan` | no | Plan o tipo de cliente. |
| `locale` | no | Idioma, p. ej. `es-ES`. El widget lo usa para sus textos. |

Reglas de validación:

- Solo se acepta HS256. Cualquier otro algoritmo, incluido `none`, se rechaza.
- Se toleran `JWT_LEEWAY_SECONDS` (30 s) de desfase de reloj.
- Un token de la app A nunca vale para la app B, porque cada app tiene su secreto.

Respuestas de error:

- `401 {"detail": "Token caducado"}`: el widget pide un token nuevo a tu app y reintenta.
- `401` en cualquier otro caso de token inválido o ausente.
- `403`: la petición llega con una cabecera `Origin` que no pertenece a la app del token.

Para rotar el secreto (los tokens anteriores dejan de valer):

```bash
python -m app.cli update-app --slug mi-app --rotate-jwt-secret
```

## CORS

Cada app declara los orígenes desde los que carga el widget:

```bash
python -m app.cli create-app --slug mi-app --name "Mi App" --allowed-origin https://app.midominio.com
python -m app.cli update-app --slug mi-app --allowed-origin https://app.midominio.com --allowed-origin https://beta.midominio.com
```

- El preflight `OPTIONS` todavía no lleva el JWT, así que se acepta si el origen
  pertenece a **alguna** app. Después, cada petición real se rechaza con `403` si el
  origen no pertenece a la app del token.
- Solo `/v1/*` tiene CORS. `/admin` es servidor a servidor y nunca responde a otros
  orígenes.
- Los orígenes se cachean `CORS_CACHE_SECONDS` (30 s). Un cambio tarda como mucho eso en
  aplicarse.

## Rate limiting

Hay dos contadores en Redis, con ventana fija de un minuto. Se comparten entre
instancias de la API.

| Límite | Variable | Por defecto |
|---|---|---|
| Por usuario (app + `user_id`) | `RATE_LIMIT_USER_PER_MINUTE` | 30 |
| Por IP | `RATE_LIMIT_IP_PER_MINUTE` | 120 |

Al superarse, el servicio responde `429` con `Retry-After` (los segundos que faltan para
la siguiente ventana). Si Redis no responde, se deja pasar la petición y se registra un
aviso: una caída de Redis no tumba el soporte.

Detrás de un proxy inverso, define `FORWARDED_ALLOW_IPS` con la IP del proxy para que el
servicio vea la IP real del cliente (ver [despliegue](despliegue.md)).

## Endpoints

### `POST /v1/conversations`

Devuelve la conversación actual del usuario con su historial. Si hay una humana abierta,
devuelve esa; si no, la abierta con el bot, o crea una nueva.

```json
{"id": "…", "kind": "bot", "status": "open", "messages": [
  {"id": 1, "role": "user", "content": "Hola", "author_name": null, "rating": null, "created_at": "…"}
]}
```

`role` puede ser `user`, `assistant` (el bot) o `agent` (una persona). El widget no ve los
datos internos: tokens, chunks, flags ni salida literal del modelo.

### `GET /v1/conversations/{id}/messages?after=<id>`

Historial completo, o solo los mensajes posteriores a `after`.

### `POST /v1/conversations/{id}/messages`

Cuerpo: `{"content": "…"}` (de 1 a 4000 caracteres).

- **Conversación con el bot:** responde en **SSE** (`text/event-stream`):

  ```
  event: token
  data: {"text": "Puedes pagar"}

  event: token
  data: {"text": " con tarjeta."}

  event: done
  data: {"message_id": 12, "escalated": false, "human_conversation_id": null}
  ```

  - `done` con `escalated: true` indica que el bot ha escalado y que
    `human_conversation_id` es la nueva conversación humana.
  - Si el LLM falla o da timeout, en lugar de `done` llega
    `event: error` con `{"message_id", "message", "offer_human": true}`. El widget muestra
    el mensaje y un botón para hablar con una persona.
- **Conversación humana:** guarda el mensaje, avisa a tu app con el webhook
  `message.created` y responde `201` con el mensaje.
- **Conversación cerrada:** `409`.

### `POST /v1/conversations/{id}/escalate`

Corresponde al botón «hablar con una persona». Cierra la conversación del bot, abre una
humana y dispara el webhook `conversation.escalated`. Responde
`201 {"human_conversation_id": "…"}`, o `409` si la conversación no es del bot o ya
estaba cerrada.

### `GET /v1/conversations/{id}/events?after=<id>`

Stream **SSE** con los mensajes nuevos de una conversación humana, en tiempo real:

```
id: 15
event: message
data: {"id": 15, "role": "agent", "content": "Hola, soy Luis", "author_name": "Luis", …}

event: closed
data: {"conversation_id": "…"}
```

- El servicio consulta la base de datos cada `WIDGET_POLL_INTERVAL` segundos (2). Este
  sistema funciona con varias instancias sin piezas extra.
- Envía un comentario `: ping` cada `SSE_HEARTBEAT_SECONDS` (15) para que los proxies no
  corten la conexión.
- El stream termina en tres casos:
  - con `closed`, cuando el agente cierra la conversación;
  - al caducar el JWT, y entonces el widget renueva el token y se reconecta;
  - al desconectarse el cliente.
- Para reanudar, se usa `after` o la cabecera `Last-Event-ID`, que llevan el id del
  último mensaje recibido.
- Solo es válido para conversaciones humanas. En las demás responde `409`.

### `POST /v1/messages/{id}/feedback`

Cuerpo: `{"rating": "up"}` o `{"rating": "down"}`. Solo acepta respuestas del bot dentro de
las conversaciones del propio usuario. Enviar una valoración nueva sustituye la anterior.
Responde `204`, o `404` si el mensaje no es una respuesta del bot de ese usuario.

## Notas de implementación

- Los endpoints en streaming abren su propia sesión de base de datos dentro del
  generador. La sesión de la petición usa `Depends(get_session, scope="function")`
  (`app.db.DbSession`) y se cierra al salir del endpoint, así que un SSE largo no retiene
  conexiones del pool.
- Cada sondeo del stream de eventos usa una sesión corta.

## Tests

- `tests/test_jwt.py`: firma, algoritmo, emisor, caducidad, duración máxima, claims y
  falsificación entre apps.
- `tests/test_widget_api.py`:
  - streaming, escalado y fallo del LLM;
  - flujo humano con eventos en tiempo real y fin del stream al caducar el token;
  - aislamiento entre usuarios, valoraciones y validación;
  - CORS (preflight y origen por app) y rate limit por usuario y por IP.
- `tests/test_ratelimit.py`: ventanas, claves independientes y fail-open sin Redis.
