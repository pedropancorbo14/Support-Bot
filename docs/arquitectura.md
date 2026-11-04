# Arquitectura

```
Usuario ─► Widget JS (embebido en la app cliente)
              │  JWT de corta duración, emitido por la app cliente
              ▼
        Servicio de soporte (FastAPI) ──► Ollama (red interna, sin puertos expuestos)
              │                       └──► PostgreSQL + pgvector
              │                       └──► Redis (colas y rate limit)
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

La tabla se amplía en cada fase.
