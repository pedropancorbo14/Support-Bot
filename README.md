# Support Bot

Servicio independiente de soporte con IA para una o varias apps.

- Responde a los usuarios usando **solo la documentación de cada app** (RAG con Ollama y
  pgvector).
- Cuando hace falta, **pasa la conversación a una persona** del equipo, avisando a la app
  con webhooks firmados.
- Lo registra todo para **medir, evaluar y hacer fine-tuning**.

```
Usuario ─► widget.js (en tu app) ──JWT──► Servicio (FastAPI) ──► Ollama (red interna)
                                              │             └──► PostgreSQL + pgvector, Redis
                                              └─ webhooks firmados ──► Tu app
Tu app ──API key──► /admin: documentación, conversaciones, respuestas de agentes, métricas
```

## 1. Puesta en marcha

Requisitos: Docker con Compose y, para usar GPU, el driver correspondiente (ver
[despliegue](docs/despliegue.md)).

```bash
cp .env.example .env
```

Edita `.env`: como mínimo `POSTGRES_PASSWORD`, `COMPOSE_PROFILES` (`cpu`, `nvidia`, `amd`
o `vulkan`) y `PUBLIC_BASE_URL`. Después:

```bash
docker compose up -d --build
```

Se levantan estos servicios:

| Servicio | Función |
|---|---|
| `migrate` | Aplica las migraciones y termina. |
| `api` | El servicio, en el puerto `API_PORT`. |
| `worker` | Embeddings y webhooks. |
| `postgres`, `redis`, `ollama-<perfil>` | Sin puertos expuestos. |

## 2. Descargar los modelos

Sustituye `ollama-nvidia` por el servicio de tu perfil:

```bash
docker compose exec ollama-nvidia ollama pull qwen3:8b
docker compose exec ollama-nvidia ollama pull nomic-embed-text
curl http://localhost:8000/health     # {"status": "ok", ...}
```

Para usar otro modelo de chat (por ejemplo `gemma4:12b`), cambia `LLM_CHAT_MODEL`. Cambiar
el modelo de embeddings obliga a reindexar (ver [RAG](docs/rag.md)).

## 3. Dar de alta una app y pegar el system prompt

```bash
docker compose run --rm api python -m app.cli create-app --slug mi-app --name "Mi App" \
  --allowed-origin https://mi-app.com --webhook-url https://mi-app.com/support/webhook
```

Este comando muestra **una sola vez** la API key, el secreto JWT y el secreto del webhook.
Guárdalos en tu app.

Después, pega tu system prompt en `prompts/system.md`. Admite los placeholders
`{app_name}`, `{user_name}`, `{user_plan}` y `{user_locale}`, y se relee sin reiniciar. El
prompt debe indicar al modelo que termine con `[ESCALAR: motivo]` cuando haya que pasar
con una persona (ver [conversaciones](docs/conversaciones.md)).

## 4. Indexar la documentación

Hay dos vías, ambas en Markdown o texto plano:

```bash
# Una carpeta local de .md/.txt
docker compose run --rm -v ./docs-mi-app:/docs:ro api python -m app.cli ingest --app mi-app /docs
```

```bash
# Desde tu app, documento a documento (crear/actualizar y borrar)
curl -X PUT https://soporte.midominio.com/admin/documents/faq-pagos \
  -H "X-Api-Key: sk_..." -H "Content-Type: application/json" \
  -d '{"title": "Pagos", "content": "## Tarjeta\n\nAceptamos Visa y Mastercard."}'
```

Los embeddings se generan en segundo plano. Si un documento no cambia, no se recalcula
nada.

## 5. Integrarlo en tu app

Tu app necesita tres piezas:

1. **Un endpoint que emita el JWT** del usuario con sesión iniciada. El JWT lleva `iss` (el
   slug de la app), `user_id`, `name`, `plan` y `locale`, y caduca en 15 minutos como
   máximo.
2. **El widget**, cargado en tu layout:

   ```html
   <script src="https://soporte.midominio.com/widget.js" data-token-endpoint="/support/token" defer></script>
   ```

3. **Un receptor de webhooks** que verifique la firma HMAC: escalados
   (`conversation.escalated`) y mensajes del usuario a los agentes (`message.created`).

Los agentes responden y cierran conversaciones con la API `/admin`.

👉 **Código completo para Laravel, probado de punta a punta:**
[docs/integracion-laravel.md](docs/integracion-laravel.md).

## 6. Evaluar

Rellena `eval/cases.toml` con preguntas reales y el comportamiento esperado (`answer`,
`unknown` o `escalate`, más las palabras clave). Después:

```bash
docker compose run --rm -v ./eval:/app/eval:ro api python -m app.cli evaluate --app mi-app
```

El informe muestra aciertos, escalados correctos e incorrectos, respuestas sin respaldo
y latencia media. La evaluación no deja conversaciones ni envía webhooks. Detalles en
[evaluación](docs/evaluacion.md).

## 7. Métricas y dataset

```bash
curl https://soporte.midominio.com/admin/metrics -H "X-Api-Key: sk_..."
```

```bash
docker compose run --rm -v ./exports:/exports api \
  python -m app.cli export-dataset --app mi-app --output /exports/mi-app.jsonl
```

Ver [observabilidad](docs/observabilidad.md) y [dataset](docs/dataset.md).

## Desarrollo

Requisitos: Python 3.13, [uv](https://docs.astral.sh/uv/), PostgreSQL con pgvector y Redis.

```bash
uv sync
uv run pytest -q
uv run ruff format . && uv run ruff check .
```

La API interactiva (OpenAPI) está en `/docs`.

## Documentación

| Tema | Documento |
|---|---|
| Arquitectura y decisiones | [arquitectura](docs/arquitectura.md) |
| Variables de entorno | [configuración](docs/configuracion.md) |
| Docker, GPU, proxy y CI | [despliegue](docs/despliegue.md) |
| Cliente LLM intercambiable | [cliente-llm](docs/cliente-llm.md) |
| Ingesta y recuperación | [rag](docs/rag.md) |
| Conversaciones, prompt y escalado | [conversaciones](docs/conversaciones.md) |
| Webhooks y su firma | [webhooks](docs/webhooks.md) |
| API administrativa (`/admin`) | [api-admin](docs/api-admin.md) |
| API del widget (`/v1`): JWT, SSE, CORS, rate limit | [api-widget](docs/api-widget.md) |
| Widget embebible | [widget](docs/widget.md) |
| Integración con Laravel | [integracion-laravel](docs/integracion-laravel.md) |
| Métricas, logs y salud | [observabilidad](docs/observabilidad.md) |
| Exportación para fine-tuning | [dataset](docs/dataset.md) |
| Evaluación | [evaluacion](docs/evaluacion.md) |
