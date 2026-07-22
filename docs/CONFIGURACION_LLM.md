# Configuracion De LLM

El backend puede usar cuatro proveedores:

```txt
gemini  -> Gemini API en la nube, recomendado en el droplet.
mock    -> reglas locales, rapido y sin costo.
local   -> script local_llm.py con modelo GGUF.
ollama  -> servidor Ollama compatible con chat.
```

La salida de todos los proveedores se valida contra el mismo esquema:

```txt
participants[]
removals[]
```

El LLM solo interpreta texto. El calculo de horarios lo hace Python.

## Donde Configurar

Local con `uvicorn`:

```txt
backend/.env
```

Produccion con Docker Compose:

```txt
.env.prod
docker-compose.prod.yml
```

Despues de cambiar variables, reinicia el backend.

## Produccion Recomendada

```txt
LLM_PROVIDER=gemini
GEMINI_API_KEY=tu_api_key
GEMINI_MODEL=gemini-2.5-flash-lite
GEMINI_COOLDOWN_SECONDS=60
LLM_FALLBACK_ENABLED=true
LLM_CACHE_ENABLED=true
GEMINI_INPUT_PRICE_PER_MILLION=0.10
GEMINI_OUTPUT_PRICE_PER_MILLION=0.40
```

`docker-compose.prod.yml` pasa estas variables al backend. Si no las defines,
usa defaults conservadores.

## Por Que Gemini Flash-Lite

Para este MVP la tarea principal es extraccion corta desde mensajes de WhatsApp:

- nombres;
- dias;
- rangos horarios;
- disponibilidad;
- remociones;
- ruido o mensajes fuera de dominio.

`gemini-2.5-flash-lite` es adecuado porque prioriza baja latencia y bajo costo.
Si mas adelante el bot debe razonar sobre conversaciones largas o ambiguas, se
puede comparar contra `gemini-2.5-flash` antes de cambiar produccion.

No uses nombres inventados como `gemini-3.0-flash` si el endpoint no los soporta.

## Fallback Y Cache

`LLM_FALLBACK_ENABLED=true` mantiene la demo operativa si Gemini falla:

```txt
gemini OK      -> llm_source = gemini_gemini-2.5-flash-lite
gemini falla   -> llm_source = mock_fallback_gemini_...
sin senal      -> llm_source = channel_no_new_availability
```

El fallback por reglas entiende casos comunes:

```txt
yo puedo lunes en la tarde
estoy libre lun de 3 a 5 pm
me sirve mierc 15:30-17:00
no me va bien martes de 10 a 12
ya no puedo ningun dia
```

`LLM_CACHE_ENABLED=true` evita repetir llamadas identicas al proveedor. Las
respuestas cacheadas reportan `0` tokens nuevos.

Para una prueba donde Gemini debe ser usado si o si:

```txt
LLM_PROVIDER=gemini
LLM_FALLBACK_ENABLED=false
LLM_CACHE_ENABLED=false
```

## Cooldown

Si Gemini responde `429` o `503`, el backend pausa llamadas por
`GEMINI_COOLDOWN_SECONDS`. Con fallback activo, durante esa ventana usa reglas.
Esto evita insistir contra una cuota agotada o un servicio temporalmente caido.

## Medicion De Tokens Y Costo

Cuando Gemini responde correctamente, el backend lee `usageMetadata`:

```txt
prompt_tokens
completion_tokens
total_tokens
estimated_cost_usd
```

Los precios estimados vienen de:

```txt
GEMINI_INPUT_PRICE_PER_MILLION=0.10
GEMINI_OUTPUT_PRICE_PER_MILLION=0.40
```

Son configurables para mantener la UI alineada con cambios de precio.

## Verificar Provider Activo

Local:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/runtime
```

Produccion:

```powershell
$pair = "usuario:password"
$basic = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes($pair))
$headers = @{ Authorization = "Basic $basic" }
Invoke-RestMethod https://coordina.xshift007.com/api/runtime -Headers $headers
```

Resultado esperado en produccion:

```txt
provider: gemini
provider_label: gemini:gemini-2.5-flash-lite
gemini_configured: True
fallback_enabled: True
cache_enabled: True
warnings: []
```

Si el panel muestra `gemini:gemini-2.5-flash-lite`, significa que el backend esta
configurado para usar Gemini. No significa que cada mensaje haya consumido tokens:
puede haber cache, fallback o una invocacion sin disponibilidad nueva.

## Desarrollo Sin Costo

Para desarrollo rapido:

```txt
LLM_PROVIDER=mock
DB_BACKEND=json
```

El mock es determinista y es el modo recomendado para tests.

## Modelo Local

```txt
LLM_PROVIDER=local
LOCAL_LLM_MODEL=qwen
LOCAL_LLM_SCRIPT=C:\ruta\a\local_llm.py
LOCAL_LLM_TIMEOUT_SECONDS=180
LOCAL_LLM_MAX_TOKENS=700
```

Este modo evita enviar datos a la nube, pero no es recomendado para el droplet
pequeno porque cargar modelos GGUF consume mas RAM y CPU.

No uses `LOCAL_LLM_MODEL=gemini`; Gemini se selecciona con `LLM_PROVIDER=gemini`.

## Ollama

```txt
LLM_PROVIDER=ollama
OLLAMA_MODEL=llama3.1
OLLAMA_URL=http://localhost:11434/api/chat
```

Util si ya tienes Ollama corriendo. No se usa en el despliegue actual.

## Problemas Frecuentes

### El panel dice Gemini activo pero no recuerdo haber puesto key

Revisa `.env.prod` en el droplet o `backend/.env` local. El frontend nunca recibe
la key; solo ve `gemini_configured: true/false`.

### `mock_fallback_gemini`

Gemini fallo o no estaba disponible, y el backend uso reglas. Mira logs del
backend y revisa cuota/key.

### `channel_no_new_availability`

La invocacion no tenia disponibilidad nueva. Es correcto para ruido como:

```txt
@coordina jajaj asdf $$$ 123
```

### Costos no aparecen

Solo aparecen si Gemini respondio realmente. Cache, mock y fallback no tienen
uso real de tokens de Gemini.
