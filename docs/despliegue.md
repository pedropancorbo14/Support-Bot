# Despliegue

## Servicios de `docker-compose.yml`

| Servicio | Imagen | Puertos publicados | Notas |
|---|---|---|---|
| `migrate` | build local | ninguno | Aplica las migraciones (`alembic upgrade head`) y termina. |
| `api` | build local | `${API_PORT:-8000}` | Uvicorn. Arranca cuando `migrate` termina bien. Monta `./prompts` (solo lectura) para editar el system prompt sin reconstruir. |
| `worker` | build local | ninguno | `python -m app.worker`: envía webhooks y genera embeddings. Se puede escalar (`--scale worker=3`). |
| `postgres` | `pgvector/pgvector:pg17` | ninguno | Volumen `postgres`. |
| `redis` | `redis:7-alpine` | ninguno | Contadores del rate limit (sin persistencia). |
| `ollama-<perfil>` | `ollama/ollama` | ninguno | Alias de red `ollama`. Volumen `ollama` con los modelos. |

Todos comparten la red `backend`. Solo `api` publica un puerto, así que Ollama,
PostgreSQL y Redis no son accesibles desde fuera del host Docker.

## Detrás de un proxy inverso (HTTPS)

En producción, pon el servicio detrás de un proxy (Nginx, Caddy, Traefik…) con HTTPS.

- **IP real del cliente:** define `FORWARDED_ALLOW_IPS` con la IP del proxy tal como la
  ve el contenedor. Si el proxy corre en el host, suele ser la puerta de enlace de la red
  Docker (por ejemplo `172.18.0.1`). Si no, el rate limit por IP vería a todos los
  usuarios como una sola IP. No uses `*` si el puerto del servicio es accesible
  directamente, porque cualquiera podría falsear `X-Forwarded-For`.
- **SSE:** desactiva el buffering para `/v1/` (el servicio ya envía
  `X-Accel-Buffering: no`, que Nginx respeta) y sube el timeout de lectura por encima de
  `SSE_HEARTBEAT_SECONDS`.

## Elegir la GPU

Con `COMPOSE_PROFILES` en `.env` se elige qué variante de Ollama arranca:

| Perfil | Imagen | Requisitos en el host |
|---|---|---|
| `cpu` | `ollama/ollama:latest` | Ninguno (lento, útil en desarrollo). |
| `nvidia` | `ollama/ollama:latest` | Driver NVIDIA + NVIDIA Container Toolkit. |
| `amd` | `ollama/ollama:rocm` | ROCm (expone `/dev/kfd` y `/dev/dri`). |
| `vulkan` | `ollama/ollama:latest` con `OLLAMA_VULKAN=1` | Driver Vulkan (expone `/dev/dri`). |

## Puesta en marcha

```bash
cp .env.example .env        # y edita contraseñas y perfil
docker compose up -d --build
docker compose exec ollama-nvidia ollama pull qwen3:8b          # usa tu perfil
docker compose exec ollama-nvidia ollama pull nomic-embed-text
curl http://localhost:8000/health
```

`/health` responde `200` si la base de datos y Ollama contestan, y `503` si alguno falla.

## Dar de alta una app e indexar su documentación

```bash
docker compose run --rm api python -m app.cli create-app --slug mi-app --name "Mi App" \
  --webhook-url https://mi-app.com/support/webhook --allowed-origin https://mi-app.com
docker compose run --rm -v ./docs-mi-app:/docs:ro api python -m app.cli ingest --app mi-app /docs
```

El primer comando muestra la API key, el secreto JWT y el secreto del webhook **una sola
vez**. Guárdalos en la app cliente. Los detalles están en [ingesta y recuperación](rag.md)
y [API del widget](api-widget.md).

## Integración continua

`.github/workflows/ci.yml` se ejecuta en cada push y en cada pull request. Levanta
`pgvector/pgvector:pg17` y `redis:7-alpine`, instala dependencias con `uv sync --frozen`, comprueba formato
y lint con Ruff, aplica las migraciones y lanza `pytest`.
