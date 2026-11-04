# Conversaciones, prompt y escalado

Toda la lógica está en `app/chat/`:

| Módulo | Responsabilidad |
|---|---|
| `service.py` | `ChatService`: respuesta del bot, escalado y chat con agentes humanos. |
| `prompt.py` | System prompt editable, montaje del prompt, historial y resumen. |
| `escalation.py` | Detección y eliminación de `[ESCALAR: motivo]`, también en streaming. |

## Modelo de datos

**`conversations`**

| Campo | Descripción |
|---|---|
| `id` | UUID. Es el identificador público. |
| `app_id` | App cliente a la que pertenece. |
| `kind` | `bot` o `human`. |
| `status` | `open` o `closed`. |
| `parent_id` | Si es humana, la conversación del bot de la que procede. |
| `external_user_id`, `user_name`, `user_plan`, `user_locale` | Datos del usuario tomados del JWT. |
| `summary`, `summary_until_message_id` | Resumen de los mensajes antiguos y hasta qué mensaje cubre. |
| `escalation_reason`, `escalated_by` | Motivo del escalado y quién lo pidió: `model` o `user`. |

**`messages`**

| Campo | Descripción |
|---|---|
| `role` | `user`, `assistant` (el bot) o `agent` (una persona). |
| `content` | Lo que ve el usuario, ya sin la etiqueta de escalado. |
| `raw_content` | Salida literal del modelo, con la etiqueta si la había. |
| `author_name` | Nombre del agente humano. |
| `prompt_tokens`, `completion_tokens` | Tokens reales que informa el proveedor. |
| `latency_ms`, `ttft_ms` | Latencia total y hasta el primer token visible. |
| `retrieved_chunks` | `[{chunk_id, document, section, score}]` usados en la respuesta. |
| `flags` | `escalated`, `no_context` (no hubo documentación) o `llm_error`. |

## Ciclo de vida

```
            start_conversation
                   │
                   ▼
   ┌──────── bot / open ────────┐
   │  reply() …                 │  [ESCALAR: …] del modelo
   │                            │  o escalate() del usuario
   │                            ▼
   │                   bot / closed ──► human / open ──► human / closed
   │                       (webhook                 (el agente
   │                  conversation.escalated)       llama a close)
```

- `start_conversation` devuelve la conversación abierta del usuario, con prioridad para
  la humana. Si no hay ninguna, crea una con el bot. Cuando el agente cierra la
  humana, la siguiente llamada crea otra con el bot.
- En una conversación humana, los mensajes del usuario se guardan y se notifican a la
  app con el webhook `message.created`. El bot no responde.
- Las operaciones no válidas lanzan `ConversationStateError`. Por ejemplo: que el bot
  responda en una conversación humana, escribir en una cerrada o escalar dos veces.

## Una respuesta del bot, paso a paso (`ChatService.reply`)

1. Se carga el historial y se guarda el mensaje del usuario (commit). Así queda
   registrado aunque el cliente corte el stream.
2. El historial se divide en *recientes* (los últimos `HISTORY_MAX_MESSAGES` que caben en
   `HISTORY_MAX_TOKENS`) y *antiguos*. Si hay antiguos sin resumir, se actualiza
   `conversation.summary` con el LLM. Si esa llamada falla, se sigue con el resumen
   anterior.
3. Se recuperan los fragmentos de documentación de la app (ver [RAG](rag.md)).
4. Se monta el prompt y se hace streaming. Cada fragmento pasa por `EscalationFilter`
   antes de emitirse.
5. Se guarda la respuesta con sus métricas. Si había etiqueta, se escala en la misma
   transacción.

Los eventos que emite `reply()` son los que la fase 4 traducirá a SSE:

| Evento | Cuándo |
|---|---|
| `Token(text)` | Cada fragmento visible. |
| `Completed(message_id, escalated, human_conversation_id)` | Fin normal. |
| `Failed(message_id, message)` | El LLM falló o dio timeout. `message` es un texto amable que ofrece hablar con una persona. |

Las respuestas fallidas se guardan con el flag `llm_error` y no se incluyen en el
historial de las siguientes preguntas.

## Prompt

Orden de los mensajes:

1. **`system`** (un único mensaje, porque algunas plantillas como Gemma no admiten varios):
   1. el system prompt de `prompts/system.md`, siempre primero para aprovechar la caché de
      prefijo,
   2. `## Datos del usuario` (nombre, plan e idioma del JWT),
   3. `## Documentación` (fragmentos `[Documento > Sección] contenido` o el aviso de que
      no hay documentación relevante),
   4. `## Resumen de la conversación anterior` (solo si existe).
2. Historial reciente literal (`user` / `assistant`).
3. Pregunta actual (`user`).

### `prompts/system.md`

- Es un fichero editable. El servicio lo **relee al cambiar** (compara la fecha de
  modificación), sin reiniciar. En Docker se monta como volumen de solo lectura.
- Los comentarios HTML `<!-- … -->` se eliminan antes de enviarlo al modelo, así que
  puedes dejar notas internas.
- Placeholders: `{app_name}`, `{user_name}`, `{user_plan}`, `{user_locale}`. Solo se
  sustituyen esos cuatro; cualquier otra llave (`{"json": true}`) se respeta.
- Para que la caché de prefijo funcione entre usuarios, usa solo `{app_name}`. Los datos
  del usuario ya se añaden justo después.
- Mientras el fichero esté vacío, o solo tenga comentarios, se registra un aviso en el log.

## Escalado

El modelo debe terminar con `[ESCALAR: motivo]` cuando haya que pasar con una persona.

- **Detección en streaming:** `EscalationFilter` retiene el texto desde cualquier `[` que
  pueda iniciar la etiqueta hasta saber si lo es. La etiqueta nunca llega al usuario,
  aunque venga partida entre tokens. Otros corchetes (`[Guardar]`, `[ESC]`) se muestran
  con normalidad.
- No distingue mayúsculas de minúsculas. Una etiqueta sin `]` al final del stream también
  cuenta. Si hay varias, se eliminan todas y se guarda el primer motivo.
- **Al escalar:** la conversación del bot se cierra con el motivo, se crea la humana
  enlazada y se encola el webhook `conversation.escalated` (ver [webhooks](webhooks.md)).
- El usuario puede pedir una persona en cualquier momento (`ChatService.escalate`). Se
  dispara el mismo webhook, con `escalated_by: "user"`.

## Tests

- `tests/test_escalation.py`: etiqueta partida con tamaños de fragmento de 1 a 1000
  caracteres, corchetes ajenos, mayúsculas, etiqueta sin cerrar y motivo vacío.
- `tests/test_prompt.py`: orden del prompt, placeholders, comentarios, recarga del
  fichero y división del historial.
- `tests/test_chat_service.py`: métricas, contexto, aviso sin documentación, escalado
  por el modelo y por el usuario, fallo del LLM, resumen del historial, flujo humano,
  reglas de estado y aislamiento entre apps.
