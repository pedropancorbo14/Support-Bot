# CLAUDE.md

Guía para trabajar en este repositorio. Léela antes de tocar código.

## Qué es

Servicio independiente de soporte con IA. Responde a los usuarios de una o varias apps
cliente usando **solo** su documentación (RAG). Escala a un agente humano (con chat humano
en el propio servicio) y registra todo para evaluación y fine-tuning. La arquitectura y
las decisiones están en `docs/arquitectura.md`.

## Decisiones cerradas con el usuario (no reabrir)

- Stack: **Python 3.13 + FastAPI**, PostgreSQL + pgvector, Ollama. Redis solo para el
  rate limit (fase 4). Las colas van en PostgreSQL (ver `docs/arquitectura.md`).
- **Multi-app**: un mismo servicio atiende a varias apps. Cada app tiene su documentación,
  API key, secreto JWT, orígenes CORS y webhook. Nunca se mezclan datos entre apps.
- Ingesta: cada app envía su documentación en **Markdown o texto plano** por API. El CLI
  indexa una carpeta de `.md`/`.txt` para una app concreta. Sin PDF ni HTML.
- Escalado: la conversación con el bot **se cierra** y se abre una **conversación humana**
  en este mismo servicio. Los agentes contestan desde la app principal vía API. El widget
  recibe sus mensajes en tiempo real. El **agente** cierra el chat humano y después el
  usuario puede volver con el bot.
- System prompt: lo aportará el usuario. Va en `prompts/system.md` con placeholders.
  No inventar su contenido.
- **Solo los ficheros estrictamente necesarios.** Nada de ficheros de ejemplo, overrides
  ni módulos vacíos.
- **Una rama por fase**, con nombre descriptivo. Cada rama sale de la anterior:
  1. `fase-1-esqueleto-docker-configuracion-cliente-llm` ✅
  2. `fase-2-ingesta-documentacion-y-recuperacion-rag` ✅
  3. `fase-3-conversaciones-prompt-escalado-webhooks` ✅
  4. `fase-4-autenticacion-jwt-api-streaming-widget` ✅
  5. `fase-5-observabilidad-exportacion-evaluacion-readme` ✅
- Documentar todo lo que se haga en `docs/` (y mantener este fichero al día).
- Idioma: código e identificadores en inglés; documentación, mensajes al usuario y
  docstrings en español.

## Comandos

```bash
uv sync                                   # instalar dependencias
uv run pytest -q                          # tests (necesitan PostgreSQL con pgvector)
uv run ruff format . && uv run ruff check --fix .
uv run alembic upgrade head               # migraciones
uv run alembic revision -m "mensaje"      # nueva migración (numerar 000N_*.py a mano)
uv run uvicorn app.main:app --reload      # servidor local
```

Fixtures útiles (`tests/conftest.py`): `sessionmaker`/`session` (vacían la base al empezar),
`demo_app`/`other_app` (`(ClientApp, api_key)`), `client` (httpx contra la app), `fake_llm`;
helpers `make_token(app, **claims)` y `bearer(token)` para el widget.

Los tests también necesitan Redis (`TEST_REDIS_URL`, por defecto `redis://localhost:6379/15`,
que se vacía en cada test). En local: `redis-server --daemonize yes`.

Los tests usan `TEST_DATABASE_URL`, por defecto
`postgresql+asyncpg://support:support@localhost:5432/support_test`. Esa base debe existir
y tener las migraciones aplicadas (`DATABASE_URL=<esa url> uv run alembic upgrade head`).

## Convenciones

- `app/main.py:create_app(settings, llm)` es la factoría. El estado compartido (settings,
  sessionmaker, llm) vive en `app.state` y se crea en el `lifespan`.
- Configuración solo en `app/config.py` (pydantic-settings). Cada variable nueva se
  documenta en `.env.example` y en `docs/configuracion.md`.
- Todo acceso al LLM pasa por el protocolo `LlmClient` (`app/llm.py`). En los tests se
  usa `tests/fakes.py:FakeLlmClient` (embeddings deterministas de bolsa de palabras).
  Los tests nunca llaman a Ollama.
- Los errores del proveedor se envuelven en `LlmError`.
- Todo dato pertenece a una app (`ClientApp`, tabla `apps`). Toda consulta filtra por
  `app_id`: nunca devolver ni tocar datos de otra app (hay tests de aislamiento).
- Trabajo en segundo plano = filas pendientes en PostgreSQL + `FOR UPDATE SKIP LOCKED` en
  `app/worker.py`. No añadir otro sistema de colas.
- La lógica de conversación vive en `app/chat/service.py:ChatService`. Los routers solo
  traducen HTTP ↔ servicio (`get_chat_service` como dependencia). Errores de estado →
  `ConversationStateError` (→ 409); conversación ajena o inexistente → `LookupError` (→ 404).
- Webhooks: `app.webhooks.enqueue` dentro de la transacción del cambio. Nunca enviar HTTP
  desde la petición del usuario; lo hace el worker.
- El contenido de `prompts/system.md` lo decide el usuario: no escribir instrucciones ahí.
- SQLAlchemy 2.1: usar `any_()` (no `ARRAY.any()`, obsoleto). En tests, no usar
  `session.expire_all()` con objetos que se sigan usando (provoca lazy-load fuera de greenlet);
  usar `execution_options(populate_existing=True)`.
- Sesiones en endpoints: siempre `app.db.DbSession` (`scope="function"`). En respuestas en
  streaming, abrir una sesión propia dentro del generador con `app.state.sessionmaker`.
- Widget (`/v1`): dependencia `RateLimitedUser` (JWT + rate limit). Un usuario solo ve sus
  conversaciones (`ChatService.get_user_conversation`). Nunca exponer al widget campos
  internos (tokens, chunks, flags, `raw_content`).
- `static/widget.js`: JS sin dependencias ni build, Shadow DOM, SSE con `fetch`. Probar
  cambios con Playwright (Chromium preinstalado) contra el servicio con `FakeLlmClient`.
- Cada respuesta guarda `messages.context` (documentación literal inyectada): el dataset
  depende de ello. No eliminarlo.
- La evaluación (`app/evaluation.py`) corre en una transacción con
  `join_transaction_mode="create_savepoint"` que se deshace: no debe dejar datos ni webhooks.
- `docs/integracion-laravel.md` contiene código Laravel probado de punta a punta (Laravel 12
  real + servicio + worker + Playwright). Si cambia el contrato (JWT, firma, endpoints),
  actualizarlo y volver a probarlo.
- Tokens estimados con `app.llm.estimate_tokens` (≈4 caracteres/token). Los reales salen del `usage`.
- Tests asíncronos con `pytest.mark.anyio` (sin pytest-asyncio). Cliente HTTP con
  `httpx.ASGITransport` dentro de `app.router.lifespan_context(app)`.
- Migraciones Alembic con ids secuenciales (`0001`, `0002`…). Configuración en
  `[tool.alembic]` de `pyproject.toml` (no hay `alembic.ini`).
- Ruff (`line-length = 100`) debe pasar antes de cada commit. El CI
  (`.github/workflows/ci.yml`) ejecuta Ruff, las migraciones y pytest en cada push y PR.
- Commits en español, descriptivos. Fase terminada = tests en verde + docs actualizadas.

## Estado

- **Fase 1 (hecha):** configuración, cliente LLM (chat, streaming, embeddings, ping),
  `/health`, Alembic con pgvector, Dockerfile y compose con perfiles cpu/nvidia/amd/vulkan.
- **Fase 2 (hecha):** tablas `apps`, `documents`, `chunks` (vector + HNSW), alta de apps
  con API key (hash SHA-256), chunker por secciones, ingesta con diff por hash, worker de
  embeddings con cola en PostgreSQL, recuperación por coseno con umbral, `PUT/DELETE
  /admin/documents/{external_id}`, CLI `create-app` e `ingest`, servicios `migrate` y `worker`.
- **Fase 3 (hecha):** tablas `conversations`, `messages`, `webhook_deliveries`;
  `ChatService` (bot con streaming y métricas, resumen de historial, escalado por modelo o
  usuario, conversación humana, cierre por el agente); `prompts/system.md` con
  placeholders y recarga en caliente; filtro de `[ESCALAR]` en streaming; webhooks
  `conversation.escalated` y `message.created` firmados (HMAC) con reintentos y resumen
  generado en el worker; endpoints admin de conversaciones; CLI `update-app`.
- **Fase 4 (hecha):** JWT HS256 por app (`iss` = slug, `user_id`, `name`, `plan`,
  `locale`, vida ≤ 15 min), API `/v1` con SSE (respuesta del bot y mensajes del agente en
  tiempo real con sondeo), escalado desde el widget, valoración 👍/👎, rate limit por
  usuario e IP en Redis (fail-open), CORS por app, `/health` con Redis, `widget.js`
  (verificado en Chromium), CLI con `--allowed-origin` y rotación de secretos.
- **Fase 5 (hecha):** `GET /admin/metrics` (agregados por app y periodo), línea de log por
  respuesta y `LOG_LEVEL`, `messages.context`, CLI `export-dataset` (JSONL chat para
  Unsloth con contexto RAG en el mensaje de usuario) y `evaluate` (`eval/cases.toml`,
  informe, `--min-accuracy`, sin efectos secundarios), README completo y guía de
  integración Laravel verificada.
- Pendiente del usuario: el texto de `prompts/system.md`.
