# CLAUDE.md

Guía para trabajar en este repositorio. Léela antes de tocar código.

## Qué es

Servicio independiente de soporte con IA. Responde a los usuarios de una o varias apps
cliente usando **solo** su documentación (RAG). Escala a un agente humano (con chat humano
en el propio servicio) y registra todo para evaluación y fine-tuning. La arquitectura y
las decisiones están en `docs/arquitectura.md`.

## Decisiones cerradas con el usuario (no reabrir)

- Stack: **Python 3.13 + FastAPI**, PostgreSQL + pgvector, Redis (colas y rate limit, desde la fase 2), Ollama.
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
  2. `fase-2-ingesta-documentacion-y-recuperacion-rag`
  3. `fase-3-conversaciones-prompt-escalado-webhooks`
  4. `fase-4-autenticacion-jwt-api-streaming-widget`
  5. `fase-5-observabilidad-exportacion-evaluacion-readme`
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
- Pendiente para la fase 2: Redis y worker en compose, tablas `apps`, `documents` y `chunks`.
