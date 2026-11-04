# Support Bot

Servicio independiente de soporte con IA. Responde a los usuarios de tus apps usando solo
su documentación (RAG con Ollama + pgvector), escala a agentes humanos y lo registra todo
para evaluarlo y hacer fine-tuning.

> En desarrollo por fases. La guía completa de integración llegará en la fase 5.

## Puesta en marcha rápida

```bash
cp .env.example .env
docker compose up -d --build
docker compose exec ollama-nvidia ollama pull qwen3:8b
docker compose exec ollama-nvidia ollama pull nomic-embed-text
curl http://localhost:8000/health
```

## Desarrollo

```bash
uv sync
uv run pytest -q
```

## Documentación

- [Arquitectura](docs/arquitectura.md)
- [Configuración](docs/configuracion.md)
- [Cliente LLM](docs/cliente-llm.md)
- [Despliegue](docs/despliegue.md)
