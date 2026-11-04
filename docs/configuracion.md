# Configuración

Toda la configuración se lee de variables de entorno (o de `.env`) en `app/config.py`.
La referencia comentada está en `.env.example`.

## Docker Compose

| Variable | Por defecto | Descripción |
|---|---|---|
| `COMPOSE_PROFILES` | – | Perfil de Ollama: `cpu`, `nvidia`, `amd` (ROCm) o `vulkan`. |
| `API_PORT` | `8000` | Puerto público del servicio, el único expuesto. |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | – | Credenciales de PostgreSQL. Compose construye `DATABASE_URL` con ellas. |
| `OLLAMA_KEEP_ALIVE` | `24h` | Tiempo que Ollama mantiene los modelos cargados. |

## Servicio

| Variable | Por defecto | Descripción |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://support:support@localhost:5432/support` | Conexión a PostgreSQL (driver asyncpg). |
| `LLM_BASE_URL` | `http://ollama:11434/v1` | Base de la API compatible con OpenAI. |
| `LLM_API_KEY` | vacío | Se envía como `Authorization: Bearer` solo si tiene valor. |
| `LLM_CHAT_MODEL` | `qwen3:8b` | Modelo de chat (alternativa: `gemma4:12b`). |
| `LLM_EMBEDDING_MODEL` | `nomic-embed-text` | Modelo de embeddings. Cambiarlo obliga a reindexar. |
| `LLM_TEMPERATURE` | `0.3` | Temperatura del chat (0–2). |
| `LLM_NUM_CTX` | `8192` | Ventana de contexto. Se pasa a Ollama como `OLLAMA_CONTEXT_LENGTH` y el servicio la usa para presupuestar el prompt. |
| `LLM_TIMEOUT` | `60` | Timeout por petición al LLM, en segundos. |
| `LLM_REASONING_EFFORT` | vacío | Si tiene valor, se envía como `reasoning_effort`. `none` desactiva el razonamiento de qwen3 y reduce la latencia. |

### Por qué `num_ctx` va por variable de entorno de Ollama

La API compatible con OpenAI no tiene un parámetro para el tamaño de contexto, así que se
configura en el servidor de Ollama (`OLLAMA_CONTEXT_LENGTH`). Compose lo rellena con el
mismo `LLM_NUM_CTX` que usa el servicio, para que ambos coincidan.
