# Arquitectura

```
Usuario ─► Widget JS (embebido en la app cliente)
              │  JWT de corta duración, emitido por la app cliente
              ▼
        Servicio de soporte (FastAPI) ──► Ollama (red interna, sin puertos expuestos)
              │                       └──► PostgreSQL + pgvector
              │                       └──► Redis (rate limit, fase 4)
              └─ webhooks firmados (HMAC) ──► App cliente (escalados, mensajes al agente)

App cliente ──► API admin del servicio (API key): documentación, conversaciones,
                respuestas de agentes humanos, métricas
```

## Multi-app

Un mismo despliegue atiende a varias **apps cliente**. Cada app tiene, por separado:

- su documentación (los chunks solo se recuperan dentro de la app),
- su API key para los endpoints administrativos,
- su secreto para firmar los JWT de sus usuarios,
- sus orígenes permitidos (CORS),
- su URL y secreto de webhook.

La app se identifica por la API key (llamadas administrativas) o por el JWT (widget).

## Ciclo de vida de una conversación

1. El usuario abre el widget y habla con el **bot** (conversación de tipo `bot`).
2. La conversación se escala si el modelo responde con `[ESCALAR: motivo]` o si el usuario
   pulsa "hablar con una persona". Entonces:
   - la conversación del bot se **cierra** con el motivo,
   - se crea una conversación de tipo `human` enlazada a la anterior,
   - se envía un webhook firmado a la app cliente con el resumen.
3. Los agentes responden desde la app cliente a través de la API admin. El widget recibe
   sus mensajes en tiempo real y cada mensaje del usuario llega a la app por webhook.
4. El **agente** cierra la conversación humana. El usuario puede empezar otra con el bot.

## Módulos (`app/`)

| Módulo | Responsabilidad | Fase |
|---|---|---|
| `config.py` | Configuración por variables de entorno | 1 |
| `db.py` | Motor async de SQLAlchemy y sesiones | 1 |
| `models.py` | Modelos ORM | 1+ |
| `llm.py` | Protocolo `LlmClient` y cliente compatible con OpenAI | 1 |
| `api/health.py` | `GET /health` | 1 |
| `main.py` | Factoría `create_app` y `lifespan` | 1 |
| `auth.py` | Alta de apps y autenticación por `X-Api-Key` | 2 |
| `rag/chunker.py` | Troceado por secciones con solape | 2 |
| `rag/ingest.py` | Alta, actualización (diff por hash) y borrado de documentos | 2 |
| `rag/retriever.py` | Búsqueda por coseno, umbral y formato del contexto | 2 |
| `api/admin.py` | Endpoints administrativos (`/admin/...`) | 2 |
| `worker.py` | Proceso en segundo plano (webhooks y embeddings) | 2–3 |
| `cli.py` | `create-app`, `update-app` e `ingest` | 2–3 |
| `chat/service.py` | `ChatService`: bot, escalado y chat humano | 3 |
| `chat/prompt.py` | System prompt, montaje del prompt e historial | 3 |
| `chat/escalation.py` | Detección de `[ESCALAR: motivo]` en streaming | 3 |
| `webhooks.py` | Firma HMAC, cola de envíos y reintentos | 3 |

La tabla se amplía en cada fase.

## Colas: PostgreSQL en lugar de Redis

El trabajo en segundo plano usa la propia base de datos como cola (patrón *outbox*):

- Un chunk con `embedding IS NULL` es un trabajo pendiente. Se guarda en la **misma
  transacción** que el documento, así que es imposible que se guarde el documento y se
  pierda el trabajo (algo que sí puede pasar si se encola en Redis después del commit).
- El worker reclama lotes con `SELECT … FOR UPDATE SKIP LOCKED`: pueden correr varios
  workers sin pisarse. Si uno cae a mitad, la transacción se deshace y el trabajo sigue
  pendiente.
- Un índice parcial (`ix_chunks_pending`) hace que buscar pendientes sea barato.
- Los webhooks siguen el mismo patrón: `webhook_deliveries` con `status = 'pending'` y
  `next_attempt_at` vencido es la cola (índice parcial `ix_webhook_deliveries_pending`).

Redis solo se añadirá en la fase 4, para el rate limiting compartido entre instancias.
