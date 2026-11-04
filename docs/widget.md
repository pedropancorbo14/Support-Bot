# Widget embebible

`static/widget.js` es un único fichero de JavaScript sin dependencias que el servicio
sirve en `/widget.js`.

## Integración

```html
<script src="https://soporte.midominio.com/widget.js"
        data-token-endpoint="/support/token"
        defer></script>
```

| Atributo | Por defecto | Descripción |
|---|---|---|
| `data-token-endpoint` | **obligatorio** | URL de **tu app** que devuelve `{"token": "<JWT>"}` para el usuario con sesión iniciada. |
| `data-api-url` | origen del `src` | URL del servicio de soporte, si no coincide con la del script. |
| `data-title` | «Soporte» | Título de la cabecera. |
| `data-primary-color` | `#4f46e5` | Color principal (botón, cabecera, mensajes del usuario). |
| `data-primary-text-color` | `#ffffff` | Color del texto sobre el color principal. |
| `data-position` | `right` | `right` o `left`. |
| `data-locale` | `locale` del JWT, si no el del navegador | `es` o `en` para los textos del widget. |

Requisitos:

- El origen de la página debe estar en `--allowed-origin` de la app (ver [CORS](api-widget.md#cors)).
- El endpoint de token se llama con `credentials: "same-origin"` y `cache: "no-store"`,
  así que puede usar la sesión y las cookies de tu app.

## Qué hace

- **Botón flotante** que abre el panel. No pide nada al servicio hasta que el usuario lo abre.
- **Historial:** al abrir, recupera la conversación actual (`POST /v1/conversations`).
- **Respuestas del bot en streaming**, con indicador de «escribiendo» hasta el primer
  token. Interpreta un Markdown mínimo y seguro: todo se escapa, y solo se interpretan
  `**negrita**`, `` `código` `` y enlaces `http(s)` (con `rel="noopener noreferrer"`).
- **Valoración 👍/👎** en cada respuesta del bot. Se puede cambiar.
- **Botón «Hablar con una persona»** mientras se habla con el bot. Si el LLM falla, el
  mismo botón aparece junto al mensaje de error.
- **Modo humano:** tras el escalado, por el modelo o por el usuario, muestra un aviso,
  envía los mensajes al agente y recibe sus respuestas en tiempo real (SSE). Si se corta
  la conexión, se reconecta con backoff (de 1 s a 30 s).
- **Fin del chat humano:** cuando el agente cierra la conversación, muestra un aviso y el
  siguiente mensaje empieza una conversación nueva con el bot, sin borrar lo que ya se ve.
- **Token:** lo pide a tu app al abrirse y lo renueva un minuto antes del `exp`. Si el
  servicio responde 401, lo renueva y reintenta una vez. Un stream de eventos que se
  corta porque caduca el token se reabre con uno nuevo.
- **Errores:** muestra un aviso amable; con `429`, pide esperar unos segundos.
- **Accesibilidad:** el botón y el panel tienen `aria-label`, el panel es un `dialog`,
  los mensajes están en una región `aria-live`, `Esc` cierra el panel, `Enter` envía y
  `Mayús+Enter` añade un salto de línea.

## Aislamiento de estilos

Todo vive dentro de un **Shadow DOM** (`#support-bot-widget`, con `all: initial` en el
host). El CSS de tu app no afecta al widget y el del widget no se filtra a tu app. Los
colores se pasan como variables CSS (`--sb-primary`, `--sb-primary-text`).

## Por qué `fetch` y no `EventSource`

`EventSource` no permite enviar la cabecera `Authorization`. Poner el JWT en la URL lo
dejaría en logs y proxies. El widget lee el SSE con `fetch` y `ReadableStream`, que
funciona en todos los navegadores modernos.

## Verificación

El flujo completo se probó en Chromium (Playwright) contra el servicio real con un LLM
falso:

1. Saludo inicial.
2. Respuesta en streaming con Markdown y valoración 👍.
3. Escalado por el modelo sin que se vea la etiqueta.
4. Mensaje del usuario al agente y respuesta del agente en tiempo real.
5. Cierre por el agente y vuelta al bot.
6. Aislamiento de estilos frente al CSS de la página.
7. Vista móvil (390 px).
