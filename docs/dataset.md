# Dataset para fine-tuning

Las respuestas que los usuarios valoran con 👍 se pueden exportar a JSONL en formato
chat, listo para entrenar con Unsloth.

```bash
docker compose run --rm -v ./exports:/exports api \
  python -m app.cli export-dataset --app mi-app --output /exports/mi-app.jsonl [--since 2026-01-01]
```

## Qué se exporta

Una línea por cada respuesta del bot con valoración 👍 que no sea un error del LLM:

```json
{"messages": [
  {"role": "system", "content": "<system prompt>\n\n## Datos del usuario\n- Nombre: Ana\n- Plan: pro\n- Idioma: es-ES"},
  {"role": "user", "content": "Hola"},
  {"role": "assistant", "content": "¡Hola, Ana! ¿En qué te ayudo?"},
  {"role": "user", "content": "## Documentación\n[FAQ > Pagos] Aceptamos tarjeta…\n\n## Pregunta\n¿Puedo pagar con tarjeta?"},
  {"role": "assistant", "content": "Sí, aceptamos tarjeta de crédito y débito."}
]}
```

- **`system`:** tu `prompts/system.md`, con los placeholders resueltos, más los datos
  del usuario de esa conversación.
- **Historial:** los turnos anteriores de la conversación (como mucho
  `HISTORY_MAX_MESSAGES`), sin las respuestas fallidas.
- **Último mensaje del usuario:** el **contexto RAG literal** que se usó al responder
  (`messages.context`), seguido de la pregunta. Es el texto exacto de ese momento, aunque
  el documento haya cambiado después. Si no hubo documentación, aparece
  `No hay documentación relevante para esta consulta`.
- **Respuesta:** la salida literal del modelo. Si escaló, incluye `[ESCALAR: motivo]`, para
  que el modelo afinado aprenda también cuándo escalar.
- Solo se exportan datos de la app indicada.

## Entrenar con Unsloth (orientativo)

```python
from datasets import load_dataset
from trl import SFTConfig, SFTTrainer
from unsloth import FastLanguageModel

model, tokenizer = FastLanguageModel.from_pretrained(
    "unsloth/Qwen3-8B", max_seq_length=8192, load_in_4bit=True
)
model = FastLanguageModel.get_peft_model(model, r=16, lora_alpha=16)

dataset = load_dataset("json", data_files="mi-app.jsonl", split="train")
dataset = dataset.map(
    lambda batch: {
        "text": [tokenizer.apply_chat_template(m, tokenize=False) for m in batch["messages"]]
    },
    batched=True,
)

trainer = SFTTrainer(
    model=model,
    tokenizer=tokenizer,
    train_dataset=dataset,
    args=SFTConfig(
        dataset_text_field="text",
        per_device_train_batch_size=2,
        num_train_epochs=2,
        output_dir="out",
    ),
)
trainer.train()
model.save_pretrained_gguf("mi-app-qwen3", tokenizer, quantization_method="q4_k_m")  # para Ollama
```

Este fragmento es orientativo y no está probado aquí: este entorno no tiene GPU ni
Unsloth. Ajusta hiperparámetros y plantilla de chat al modelo base que elijas.

Recomendaciones:

- Revisa una muestra del JSONL antes de entrenar: un 👍 no garantiza que la respuesta sea
  perfecta.
- Ten en cuenta que el formato de entrenamiento (documentación en el mensaje del usuario)
  difiere ligeramente del de producción (documentación en el mensaje `system`).
- Evalúa siempre el modelo afinado con `evaluate` (ver [evaluación](evaluacion.md))
  antes de cambiar `LLM_CHAT_MODEL`.

## Tests

`tests/test_dataset.py`: filtro por valoración, contexto RAG literal, historial,
conservación de la etiqueta de escalado, opción sin historial, aislamiento por app y
comando del CLI.
