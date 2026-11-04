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
| `LOG_LEVEL` | `INFO` | Nivel de log de la API y del worker. Con `INFO` se registra una línea de métricas por respuesta. |
| `DATABASE_URL` | `postgresql+asyncpg://support:support@localhost:5432/support` | Conexión a PostgreSQL (driver asyncpg). |
| `LLM_BASE_URL` | `http://ollama:11434/v1` | Base de la API compatible con OpenAI. |
| `LLM_API_KEY` | vacío | Se envía como `Authorization: Bearer` solo si tiene valor. |
| `LLM_CHAT_MODEL` | `qwen3:8b` | Modelo de chat (alternativa: `gemma4:12b`). |
| `LLM_EMBEDDING_MODEL` | `nomic-embed-text` | Modelo de embeddings. Cambiarlo obliga a reindexar. |
| `LLM_TEMPERATURE` | `0.3` | Temperatura del chat (0–2). |
| `LLM_NUM_CTX` | `8192` | Ventana de contexto. Se pasa a Ollama como `OLLAMA_CONTEXT_LENGTH` y el servicio la usa para presupuestar el prompt. |
| `LLM_TIMEOUT` | `60` | Timeout por petición al LLM, en segundos. |
| `LLM_REASONING_EFFORT` | vacío | Si tiene valor, se envía como `reasoning_effort`. `none` desactiva el razonamiento de qwen3 y reduce la latencia. |

| `EMBEDDING_DIMENSIONS` | `768` | Dimensión del vector. Se fija en la migración `0002`; cambiarla exige migración nueva y reindexar. |
| `EMBEDDING_DOCUMENT_PREFIX` | `search_document: ` | Prefijo al embeber chunks (recomendado por `nomic-embed-text`). |
| `EMBEDDING_QUERY_PREFIX` | `search_query: ` | Prefijo al embeber la pregunta del usuario. |
| `CHUNK_SIZE_TOKENS` | `500` | Tamaño máximo de cada chunk. |
| `CHUNK_OVERLAP_TOKENS` | `50` | Solape entre chunks consecutivos de la misma sección. Debe ser menor que el tamaño. |
| `RAG_TOP_K` | `4` | Fragmentos recuperados por pregunta. |
| `RAG_MIN_SCORE` | `0.5` | Similitud de coseno mínima (0–1). Ajústala con la evaluación (fase 5). |
| `WORKER_POLL_INTERVAL` | `2` | Segundos entre sondeos del worker cuando no hay trabajo. Es también el primer intervalo del backoff ante fallos (se duplica hasta 60 s). |

| `SYSTEM_PROMPT_PATH` | `prompts/system.md` | Fichero del system prompt. Se relee al cambiar. |
| `HISTORY_MAX_MESSAGES` | `10` | Mensajes recientes que van literales en el prompt. |
| `HISTORY_MAX_TOKENS` | `2000` | Presupuesto de tokens para ese historial literal. Lo que no cabe se resume. |
| `PUBLIC_BASE_URL` | `http://localhost:8000` | URL pública del servicio, usada en los enlaces de los webhooks. |
| `WEBHOOK_TIMEOUT` | `10` | Timeout de cada envío de webhook, en segundos. |
| `WEBHOOK_MAX_ATTEMPTS` | `8` | Intentos antes de marcar un webhook como `failed`. |
| `WEBHOOK_RETRY_BASE_SECONDS` | `30` | Espera tras el primer fallo. Se duplica en cada intento, con un máximo de 1 h. |

| `REDIS_URL` | `redis://localhost:6379/0` | Redis para el rate limit. Compose lo fija a `redis://redis:6379/0`. |
| `JWT_MAX_TTL_SECONDS` | `900` | Vida máxima aceptada del JWT del usuario (`exp − iat`). |
| `JWT_LEEWAY_SECONDS` | `30` | Desfase de reloj tolerado al validar `exp`/`iat`. |
| `RATE_LIMIT_USER_PER_MINUTE` | `30` | Peticiones por minuto a `/v1` por usuario. |
| `RATE_LIMIT_IP_PER_MINUTE` | `120` | Peticiones por minuto a `/v1` por IP. |
| `CORS_CACHE_SECONDS` | `30` | Segundos que se cachean los orígenes permitidos de las apps. |
| `WIDGET_POLL_INTERVAL` | `2` | Frecuencia con la que el stream de eventos busca mensajes del agente. |
| `SSE_HEARTBEAT_SECONDS` | `15` | Intervalo del latido (`: ping`) de los streams SSE. |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | La lee uvicorn: IPs del proxy de las que se acepta `X-Forwarded-For`. |

### Por qué `num_ctx` va por variable de entorno de Ollama

La API compatible con OpenAI no tiene un parámetro para el tamaño de contexto, así que se
configura en el servidor de Ollama (`OLLAMA_CONTEXT_LENGTH`). Compose lo rellena con el
mismo `LLM_NUM_CTX` que usa el servicio, para que ambos coincidan.
