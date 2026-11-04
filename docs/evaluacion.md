# Evaluación

`evaluate` lanza contra el bot real (recuperación, prompt y LLM) una batería de
preguntas y muestra un informe.

```bash
docker compose run --rm -v ./eval:/app/eval api \
  python -m app.cli evaluate --app mi-app --cases eval/cases.toml [--output eval/informe.json] [--min-accuracy 0.8]
```

## Fichero de casos (`eval/cases.toml`)

```toml
unknown_markers = ["no lo sé", "no tengo información"]

[[case]]
question = "¿Qué métodos de pago aceptáis?"
expected = "answer"
keywords = ["tarjeta", "transferencia"]

[[case]]
question = "¿Cuál es la capital de Australia?"
expected = "unknown"

[[case]]
question = "Quiero hablar con una persona"
expected = "escalate"
```

| Campo | Descripción |
|---|---|
| `question` | Pregunta del usuario. |
| `expected` | `answer` (responde con la documentación), `unknown` (dice que no lo sabe) o `escalate` (escala). |
| `keywords` | Solo para `answer`: **todas** deben aparecer en la respuesta. No distingue mayúsculas ni tildes. |
| `unknown_markers` | Frases que cuentan como «no lo sé». Ajústalas a la forma en que tu system prompt pide decirlo. |

El fichero del repositorio trae dos casos genéricos que valen para cualquier app (una
pregunta ajena y una petición de una persona). Los casos `answer` dependen de la
documentación: añade los tuyos, lo ideal son 30–100 preguntas reales de usuarios. Usa un
fichero por app.

## Cómo se clasifica cada respuesta

1. Si el modelo escaló (`[ESCALAR: …]`), es **`escalate`**.
2. Si no, y la respuesta contiene alguno de los `unknown_markers`, es **`unknown`**.
3. En cualquier otro caso, es **`answer`**.
4. Si el LLM falla, es **`error`**.

Un caso es **correcto** si la clasificación coincide con `expected` y, cuando se espera
`answer`, contiene todas las palabras clave.

## Informe

```
✔   1. [answer → answer] ¿Qué métodos de pago aceptáis?
✘   2. [answer → answer] ¿Hacéis envíos?
        faltan palabras clave: 24 horas
        respuesta: 'Hacemos envíos a toda España.'
        ⚠ respuesta sin respaldo en la documentación
✔   3. [escalate → escalate] Quiero hablar con una persona

Aciertos: 2/3 (67%)
  answer    1/2
  escalate  1/1
Escalados correctos: 1 · incorrectos: 0 · no detectados: 0
Respuestas sin respaldo: 1
Errores del LLM: 0
Latencia media: 1830 ms (p95 2900 ms) · primer token: 410 ms
```

- **Escalados incorrectos:** escaló cuando no tocaba. **No detectados:** debía escalar y
  no lo hizo.
- **Respuestas sin respaldo:** el bot respondió sin que se recuperara ningún fragmento
  de documentación. Es el indicador principal de alucinación.
- `--output informe.json` guarda el resumen y el detalle de cada caso (respuesta, chunks,
  puntuación máxima y latencias).
- `--min-accuracy 0.8` hace que el comando salga con código `2` si la precisión queda por
  debajo. Sirve para usarlo en CI antes de cambiar de modelo, de prompt o de umbral.

## Sin efectos secundarios

Cada evaluación se ejecuta dentro de una transacción que se **deshace** al terminar:

- no quedan conversaciones ni mensajes;
- no afecta a `/admin/metrics`;
- no se envía **ningún webhook** de escalado a tu app.

## Para qué usarla

- Ajustar `RAG_MIN_SCORE` y `RAG_TOP_K`.
- Comparar modelos (`qwen3:8b` frente a `gemma4:12b`, o un modelo afinado).
- Validar cambios en `prompts/system.md`.

## Tests

`tests/test_evaluation.py`: clasificación, normalización, validación del fichero,
resumen, comando completo sin dejar rastro (ni conversaciones ni webhooks) y código de
salida con `--min-accuracy`.
