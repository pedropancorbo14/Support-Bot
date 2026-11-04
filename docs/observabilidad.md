# Observabilidad

## Qué se registra en cada respuesta del bot

Cada respuesta se guarda en `messages` con:

| Campo | Contenido |
|---|---|
| `prompt_tokens`, `completion_tokens` | Tokens reales que informa el proveedor (`usage`). |
| `latency_ms` | Tiempo total, desde que llega la pregunta hasta que se guarda la respuesta. |
| `ttft_ms` | Tiempo hasta el primer token **visible** para el usuario. |
| `retrieved_chunks` | `[{chunk_id, document, section, score}]`. |
| `context` | Documentación literal inyectada en el prompt (se usa en el dataset). |
| `flags` | `escalated`, `no_context` o `llm_error`. |
| `rating` | 1 (👍), -1 (👎) o vacío. |
| `raw_content` | Salida literal del modelo, con la etiqueta de escalado si la había. |

Además, el servicio escribe una línea de log por respuesta, fácil de llevar a Loki,
CloudWatch, Datadog o cualquier otro sistema:

```
INFO app.chat.service respuesta conversation=… latency_ms=1840 ttft_ms=420 prompt_tokens=1312 completion_tokens=96 chunks=3 top_score=0.712 flags=-
```

El nivel de log se configura con `LOG_LEVEL` (`INFO` por defecto).

## `GET /admin/metrics`

Métricas agregadas de la app autenticada (`X-Api-Key`) en un intervalo. Por defecto son
los últimos 30 días. Se puede acotar con `?since=` y `?until=` en ISO 8601 (`until` queda
excluido).

```json
{
  "conversations": {"bot": 120, "human": 9, "escalated_by_model": 6, "escalated_by_user": 3, "escalation_rate": 0.075},
  "messages": {"user": 410, "assistant": 380, "agent": 31},
  "answers": {
    "total": 380,
    "latency_ms": {"avg": 2100, "p50": 1900, "p95": 4200},
    "ttft_ms": {"avg": 450, "p95": 900},
    "tokens": {"prompt": 501234, "completion": 40321},
    "no_context": 22, "no_context_rate": 0.0579,
    "llm_errors": 2, "llm_error_rate": 0.0053,
    "escalated": 6
  },
  "ratings": {"up": 140, "down": 12, "satisfaction": 0.9211, "rated_rate": 0.4},
  "webhooks": {"pending": 0, "delivered": 40, "failed": 0}
}
```

Cómo leer las cifras:

- **`no_context_rate` alto:** faltan documentos, o `RAG_MIN_SCORE` es demasiado exigente.
- **`llm_error_rate` mayor que 0:** Ollama falla o da timeout (revisa `/health`, la GPU y
  `LLM_TIMEOUT`).
- **`satisfaction`:** proporción de 👍 entre las respuestas valoradas.
- **`webhooks.failed` mayor que 0:** tu app no está recibiendo webhooks. El detalle de cada
  envío está en la tabla `webhook_deliveries` (ver [webhooks](webhooks.md)).

Las proporciones valen `null` cuando no hay datos.

## `GET /health`

Comprueba PostgreSQL, Redis y Ollama. Responde `200` si todo está bien y `503` si algo
falla, indicando qué:

```json
{"status": "error", "checks": {"database": "ok", "redis": "ok", "llm": "error"}}
```

Úsalo como healthcheck del balanceador o de tu sistema de monitorización.

## Tests

`tests/test_metrics.py` comprueba los agregados sobre actividad real: respuestas,
fallos, escalados por modelo y por usuario, valoraciones, aislamiento entre apps y
ventana de tiempo.
