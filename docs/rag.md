# Ingesta y recuperación (RAG)

## Flujo

```
App cliente ── PUT /admin/documents/{id} ──┐
CLI ingest (carpeta .md/.txt) ─────────────┤
                                           ▼
                      chunker ─► diff por hash ─► chunks (embedding = NULL)
                                                         │
                                  worker (SKIP LOCKED) ◄─┘
                                         │ /v1/embeddings
                                         ▼
                                  chunks.embedding (vector, índice HNSW)
                                         │
Pregunta ─► embedding de la consulta ─► top-k por coseno ─► umbral ─► contexto
```

## Formatos

Markdown y texto plano. La app cliente envía el contenido por API. El CLI indexa los
ficheros `.md`, `.markdown` y `.txt` de una carpeta (recursivamente) e ignora el resto.

## Chunking (`app/rag/chunker.py`)

1. **Secciones:** el documento se divide por títulos Markdown (`#` a `######`). Cada
   chunk guarda la ruta de títulos (`Pagos > Tarjeta`). Los `#` dentro de bloques de
   código no cuentan como títulos. Un H1 igual al título del documento no se repite en
   la ruta.
2. **Bloques:** dentro de una sección se separa por líneas en blanco. Un bloque de código
   nunca se parte en este paso.
3. **Agrupación:** los bloques se juntan hasta `CHUNK_SIZE_TOKENS` (500). Un bloque más
   grande se parte por frases y, si hace falta, por palabras.
4. **Solape:** cada chunk empieza con las últimas palabras del anterior, hasta
   `CHUNK_OVERLAP_TOKENS` (50). El solape **nunca cruza secciones**.

Los tokens se estiman con ≈4 caracteres por token (`app.llm.estimate_tokens`). La API
compatible con OpenAI de Ollama no expone un tokenizador, y para trocear basta con una
estimación.

Cada chunk guarda contenido, sección, posición, tokens estimados y un **hash SHA-256 de
título + sección + contenido**, es decir, de todo lo que se embebe.

## Actualizaciones sin reembeber (`app/rag/ingest.py`)

- Si el título y el contenido del documento no cambian, la ingesta no hace nada (`unchanged`).
- Si cambian, se trocea de nuevo y se compara por hash. Los chunks cuyo hash ya existía
  **conservan su fila y su embedding**, y solo se actualiza su posición. Los nuevos
  entran con `embedding = NULL` y los que sobran se borran.
- Cambiar el título invalida todos los embeddings del documento, porque el título forma
  parte del texto embebido.
- `external_id` es único **por app**: dos apps pueden usar el mismo id sin conflicto.

## Embeddings en segundo plano (`app/worker.py`)

El worker toma lotes de 32 chunks pendientes con `FOR UPDATE SKIP LOCKED`, llama a
`/v1/embeddings` y guarda los vectores. El texto embebido es:

```
{EMBEDDING_DOCUMENT_PREFIX}{Título} > {Sección}
{contenido}
```

Si Ollama falla, los chunks siguen pendientes y el worker reintenta con backoff
exponencial (desde `WORKER_POLL_INTERVAL` hasta 60 s). La respuesta de la ingesta
incluye `pending_embeddings` para saber cuántos faltan.

## Recuperación (`app/rag/retriever.py`)

1. Se embebe `{EMBEDDING_QUERY_PREFIX}{pregunta}`.
2. Se buscan los `RAG_TOP_K` chunks **de la app** más cercanos por distancia de coseno
   (`<=>`, índice HNSW). Los chunks aún sin embedding no participan.
3. Se descartan los que no llegan a `RAG_MIN_SCORE` (similitud = 1 − distancia). El
   umbral se aplica después del `ORDER BY … LIMIT` para que PostgreSQL use el índice.
4. `format_context` produce el bloque que irá en el prompt:

```
[FAQ > Pagos > Tarjeta] Aceptamos Visa y Mastercard.

[Guía de envíos] Los pedidos salen en 24 horas.
```

Si ningún chunk supera el umbral, el contexto es literalmente:
`No hay documentación relevante para esta consulta`.

## Cambiar de modelo de embeddings

La dimensión del vector queda fija en la migración (`EMBEDDING_DIMENSIONS`, 768 para
`nomic-embed-text`). Para cambiar de modelo:

1. Crea una migración que cambie el tipo de `chunks.embedding` y recree `ix_chunks_embedding`.
2. Pon `chunks.embedding = NULL` para reindexar. El worker regenerará todos los vectores.

## Tests

- `tests/test_chunker.py`: secciones, código, texto plano, límites, solape y hash.
- `tests/test_ingest.py`: alta, idempotencia, reutilización de embeddings, borrado,
  aislamiento entre apps y worker (también ante fallos).
- `tests/test_retriever.py`: orden por similitud, top-k, umbral, aislamiento y formato.

Los embeddings de los tests salen de `FakeLlmClient`: una bolsa de palabras con hashing,
así que los textos con palabras en común son similares.
