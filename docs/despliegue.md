# Despliegue

## Servicios de `docker-compose.yml`

| Servicio | Imagen | Puertos publicados | Notas |
|---|---|---|---|
| `api` | build local | `${API_PORT:-8000}` | Aplica las migraciones al arrancar y luego lanza uvicorn. |
| `postgres` | `pgvector/pgvector:pg17` | ninguno | Volumen `postgres`. |
| `ollama-<perfil>` | `ollama/ollama` | ninguno | Alias de red `ollama`. Volumen `ollama` con los modelos. |

Todos comparten la red `backend`. Solo `api` publica un puerto, así que Ollama y
PostgreSQL no son accesibles desde fuera del host Docker.

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
