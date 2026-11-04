# Integración con una app Laravel

Código mínimo que necesita tu app Laravel (12) para usar el servicio de soporte. Se ha
probado de punta a punta con un proyecto Laravel 12.71 real, el servicio y el worker:

- el widget cargado desde Blade con el JWT emitido por Laravel;
- el webhook de escalado firmado, verificado por Laravel y registrado;
- la respuesta del agente enviada con `SupportClient`, que aparece en el widget;
- el cierre de la conversación por el agente.

## 1. Dar de alta la app en el servicio

```bash
docker compose run --rm api python -m app.cli create-app --slug mi-app --name "Mi App" \
  --allowed-origin https://mi-app.com \
  --webhook-url https://mi-app.com/support/webhook
```

Guarda los tres secretos que muestra. No se vuelven a enseñar.

## 2. Configuración

Instala la librería JWT:

```bash
composer require firebase/php-jwt
```

`.env`:

```dotenv
SUPPORT_URL=https://soporte.midominio.com
SUPPORT_APP_SLUG=mi-app
SUPPORT_API_KEY=sk_...
SUPPORT_JWT_SECRET=...
SUPPORT_WEBHOOK_SECRET=whsec_...
```

`config/services.php`:

```php
'support' => [
    'url' => env('SUPPORT_URL'),
    'app_slug' => env('SUPPORT_APP_SLUG'),
    'api_key' => env('SUPPORT_API_KEY'),
    'jwt_secret' => env('SUPPORT_JWT_SECRET'),
    'webhook_secret' => env('SUPPORT_WEBHOOK_SECRET'),
],
```

## 3. Emitir el JWT

`app/Http/Controllers/SupportTokenController.php`:

```php
<?php

namespace App\Http\Controllers;

use Firebase\JWT\JWT;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;

/**
 * Emite el JWT de corta duración que el widget de soporte envía al servicio.
 */
class SupportTokenController extends Controller
{
    public function __invoke(Request $request): JsonResponse
    {
        $user = $request->user();
        $now = time();

        $token = JWT::encode([
            'iss' => config('services.support.app_slug'),
            'user_id' => (string) $user->getAuthIdentifier(),
            'name' => $user->name,
            'plan' => $user->plan ?? null,
            'locale' => app()->getLocale(),
            'iat' => $now,
            'exp' => $now + 600, // 10 minutos (máximo admitido: 15)
        ], config('services.support.jwt_secret'), 'HS256');

        return response()
            ->json(['token' => $token])
            ->header('Cache-Control', 'no-store');
    }
}
```

- `iss` debe ser el slug de la app.
- `plan` es opcional: adapta `$user->plan` a tu modelo.
- `locale` decide el idioma de los textos del widget (`es` o `en`). Con
  `app()->getLocale()` se usa el idioma de tu app (`APP_LOCALE`).

## 4. Recibir y verificar el webhook

`app/Http/Controllers/SupportWebhookController.php`:

```php
<?php

namespace App\Http\Controllers;

use Illuminate\Http\Request;
use Illuminate\Http\Response;
use Illuminate\Support\Facades\Cache;
use Illuminate\Support\Facades\Log;

/**
 * Recibe los webhooks firmados del servicio de soporte.
 */
class SupportWebhookController extends Controller
{
    /** Antigüedad máxima aceptada de la firma, en segundos (anti-replay). */
    private const TOLERANCE = 300;

    public function __invoke(Request $request): Response
    {
        $payload = $request->getContent(); // cuerpo en bruto: la firma se calcula sobre él

        if (! $this->hasValidSignature($request->header('X-Support-Signature', ''), $payload)) {
            abort(401, 'Firma no válida');
        }

        // Un mismo envío puede llegar más de una vez (reintentos): deduplicar por id.
        $deliveryId = (string) $request->header('X-Support-Delivery');
        if (! Cache::add("support-webhook:{$deliveryId}", true, now()->addDay())) {
            return response()->noContent();
        }

        $event = json_decode($payload, true, flags: JSON_THROW_ON_ERROR);
        $data = $event['data'];

        match ($event['event']) {
            // Hay un usuario esperando a un agente. $data incluye user, reason, summary,
            // human_conversation_id y las URLs para consultar la conversación.
            // Aquí: avisar a tu equipo (correo, Slack, cola de tickets…), mejor en un job.
            'conversation.escalated' => Log::info('Soporte: escalado', $data),
            // El usuario ha escrito en una conversación humana.
            'message.created' => Log::info('Soporte: mensaje del usuario', $data),
            default => null,
        };

        // Responder rápido con 2xx: cualquier otro código hace que el servicio reintente.
        return response()->noContent();
    }

    private function hasValidSignature(string $header, string $payload): bool
    {
        $parts = [];
        foreach (explode(',', $header) as $part) {
            [$key, $value] = array_pad(explode('=', trim($part), 2), 2, '');
            $parts[$key] = $value;
        }

        if (! isset($parts['t'], $parts['v1']) || ! ctype_digit($parts['t'])) {
            return false;
        }
        if (abs(time() - (int) $parts['t']) > self::TOLERANCE) {
            return false;
        }

        $expected = hash_hmac(
            'sha256',
            $parts['t'].'.'.$payload,
            (string) config('services.support.webhook_secret'),
        );

        return hash_equals($expected, $parts['v1']);
    }
}
```

- La firma se calcula sobre el **cuerpo en bruto**. Por eso se usa `getContent()` y no
  `$request->all()`.
- La deduplicación usa la caché. En producción, usa un driver compartido (Redis o la base
  de datos) si tienes varios servidores.
- Lo que haya que hacer con cada evento (notificar, crear un ticket…) conviene moverlo a
  un *job* en cola, para responder enseguida.

## 5. Rutas

`routes/web.php`:

```php
Route::get('/support/token', App\Http\Controllers\SupportTokenController::class)
    ->middleware(['auth', 'throttle:30,1'])
    ->name('support.token');

Route::post('/support/webhook', App\Http\Controllers\SupportWebhookController::class)
    ->name('support.webhook');
```

El webhook no lleva token CSRF. Exclúyelo en `bootstrap/app.php`:

```php
->withMiddleware(function (Middleware $middleware): void {
    $middleware->validateCsrfTokens(except: ['support/webhook']);
})
```

El widget pide el token con `Accept: application/json`, así que un usuario sin sesión
recibe `401` en lugar de una redirección al login.

## 6. Cargar el widget

En tu layout de Blade, solo para usuarios con sesión:

```blade
@auth
    <script src="{{ config('services.support.url') }}/widget.js"
            data-token-endpoint="{{ route('support.token') }}"
            data-title="Soporte"
            data-primary-color="#4f46e5"
            defer></script>
@endauth
```

El resto de atributos están en [widget](widget.md).

## 7. Responder como agente (y sincronizar la documentación)

`app/Services/SupportClient.php`:

```php
<?php

namespace App\Services;

use Illuminate\Http\Client\PendingRequest;
use Illuminate\Support\Facades\Http;

/**
 * API administrativa del servicio de soporte (servidor a servidor, con API key).
 */
class SupportClient
{
    /** Respuesta de un agente en una conversación humana. */
    public function reply(string $conversationId, string $content, ?string $agentName = null): array
    {
        return $this->http()
            ->post("/admin/conversations/{$conversationId}/messages", [
                'content' => $content,
                'author_name' => $agentName,
            ])
            ->json();
    }

    /** El agente da por terminada la conversación; el usuario vuelve con el bot. */
    public function close(string $conversationId): void
    {
        $this->http()->post("/admin/conversations/{$conversationId}/close");
    }

    /** Conversación completa con sus mensajes. */
    public function conversation(string $conversationId): array
    {
        return $this->http()->get("/admin/conversations/{$conversationId}")->json();
    }

    /** Crea o actualiza un documento de ayuda (Markdown o texto plano). */
    public function upsertDocument(string $externalId, string $title, string $content): array
    {
        return $this->http()
            ->put('/admin/documents/'.rawurlencode($externalId), [
                'title' => $title,
                'content' => $content,
            ])
            ->json();
    }

    public function deleteDocument(string $externalId): void
    {
        $this->http()->delete('/admin/documents/'.rawurlencode($externalId));
    }

    private function http(): PendingRequest
    {
        return Http::baseUrl(config('services.support.url'))
            ->withHeaders(['X-Api-Key' => config('services.support.api_key')])
            ->acceptJson()
            ->timeout(10)
            ->throw();
    }
}
```

Uso:

```php
$support = app(App\Services\SupportClient::class);
$support->reply($humanConversationId, 'Hola Ana, ya veo el cobro duplicado.', auth()->user()->name);
$support->close($humanConversationId);

// Al guardar un artículo de ayuda en tu CMS:
$support->upsertDocument("articulo-{$article->id}", $article->title, $article->body_markdown);
```

El `human_conversation_id` llega en el webhook `conversation.escalated`. Los mensajes del
usuario en esa conversación llegan con `message.created`.
