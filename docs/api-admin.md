# API administrativa

Endpoints para que cada app cliente gestione sus datos. Todos exigen la cabecera
`X-Api-Key` con la clave de la app (la muestra `create-app`). Sin clave, o con una
clave inválida, responden `401`. Cada app solo ve y modifica sus propios datos.

La documentación OpenAPI interactiva está en `/docs` (Swagger) y en `/openapi.json`.

## Alta de una app

No hay endpoint: las apps se dan de alta desde el CLI.

```bash
python -m app.cli create-app --slug mi-app --name "Mi App"
```

Se guarda el SHA-256 de la clave, nunca la clave en claro. Si la pierdes, hay que
generar una nueva.

## `PUT /admin/documents/{external_id}`

Crea o actualiza un documento. Es idempotente: reenviar el mismo contenido no hace nada.
`external_id` es el identificador del documento en tu app (de 1 a 255 caracteres).

```http
PUT /admin/documents/faq-pagos
X-Api-Key: sk_...
Content-Type: application/json

{"title": "Preguntas sobre pagos", "content": "## Tarjeta\n\nAceptamos Visa..."}
```

Respuesta `200`:

```json
{"external_id": "faq-pagos", "status": "created", "chunks": 3, "pending_embeddings": 3}
```

| Campo | Significado |
|---|---|
| `status` | `created`, `updated` o `unchanged`. |
| `chunks` | Número de chunks del documento. |
| `pending_embeddings` | Chunks que el worker aún no ha embebido. Hasta entonces no se recuperan. |

Errores: `401` sin clave válida; `422` si `title` o `content` están vacíos.

## `DELETE /admin/documents/{external_id}`

Borra el documento y todos sus chunks. Responde `204`, o `404` si el documento no
existe en tu app.

```bash
curl -X DELETE https://soporte.midominio.com/admin/documents/faq-pagos -H "X-Api-Key: sk_..."
```
