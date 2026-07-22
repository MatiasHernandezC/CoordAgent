# Configuracion de LLM

El backend decide que modelo usar mediante `backend/.env`.

Si `backend/.env` no existe, el proyecto usa los valores por defecto definidos en `backend/app/settings.py`. Para evitar confusiones, crea el archivo copiando `backend/.env.example`.

Importante: edita `backend/.env`, no `backend/.env.example`. Despues de cambiar el provider, reinicia FastAPI.

## Configuracion por defecto

El proyecto queda configurado por defecto para usar Qwen local:

```txt
LLM_PROVIDER=local
LOCAL_LLM_MODEL=qwen
LLM_FALLBACK_ENABLED=false
```

Con `LLM_FALLBACK_ENABLED=false`, si Qwen no carga o falla, el backend muestra el error en vez de ocultarlo usando `mock`.

## Opciones

### Demo rapida sin IA real

```txt
LLM_PROVIDER=mock
```

Usa reglas locales. Es lo mas rapido y sirve para probar la interfaz.

### Modelo local GGUF

```txt
LLM_PROVIDER=local
LOCAL_LLM_MODEL=qwen
```

Usa `local_llm.py`. Es privado, pero puede tardar porque carga el modelo local.

Alternativa:

```txt
LOCAL_LLM_MODEL=phi
```

### Gemini API

```txt
LLM_PROVIDER=gemini
GEMINI_API_KEY=tu_api_key
GEMINI_MODEL=gemini-2.5-flash-lite
GEMINI_COOLDOWN_SECONDS=60
```

El backend no expone la API key al frontend. Solo informa si Gemini esta configurado o no.

No uses `LOCAL_LLM_MODEL=gemini` para seleccionar Gemini. Esa variable solo sirve cuando `LLM_PROVIDER=local`.

Para obligar a que Gemini sea realmente usado y evitar que una falla se oculte con `mock_fallback`, configura:

```txt
LLM_FALLBACK_ENABLED=false
```

Si ademas quieres que cada prueba haga una llamada real y no use cache:

```txt
LLM_CACHE_ENABLED=false
```

No uses `gemini-3.0-flash`: ese nombre no esta disponible para el endpoint `v1beta/models/{model}:generateContent`. Si Gemini responde `429` con `limit: 0`, normalmente no significa que el proyecto haya gastado miles de tokens; significa que esa key/proyecto no tiene cuota free tier disponible para ese modelo o que la cuota quedo bloqueada por configuracion/billing. En esta demo se recomienda `gemini-2.5-flash-lite` porque es el modelo de texto mas barato de la familia Flash-Lite y permite medir tokens/costo con menor riesgo.

Para evitar gastar cuota por accidente, deja `LLM_PROVIDER=mock` o `LLM_PROVIDER=local` durante desarrollo y cambia a `gemini` solo para la prueba que quieras mostrar. Si Gemini devuelve `429` o `503`, el backend pausa temporalmente llamadas a Gemini y usa `mock_fallback` durante `GEMINI_COOLDOWN_SECONDS`.

## Medir tokens y costo con Gemini

La medicion real aparece solo cuando Gemini responde correctamente. El backend lee `usageMetadata` de la respuesta de `generateContent` y calcula:

- `prompt_tokens`: tokens de entrada.
- `completion_tokens`: tokens de salida.
- `total_tokens`: suma informada por Gemini.
- `estimated_cost_usd`: costo aproximado segun los precios configurados.

La interfaz muestra la ultima llamada y el total acumulado de la sesion. Las respuestas desde cache cuentan como `0 tokens`, porque no vuelven a llamar a Gemini. Para una prueba de costo real, usa `LLM_CACHE_ENABLED=false`.

Para probar:

```powershell
cd "C:\Users\cocan\Downloads\(Ultimos ramos)\TAVI\Proyecto\TAVI-Charlie\coordinador-simple-mvp\backend"
uvicorn app.main:app --reload --port 8000
```

En otra terminal:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/runtime
```

Debe mostrar:

```txt
provider       : gemini
provider_label : gemini:gemini-2.5-flash-lite
gemini_configured : True
```

Luego usa el frontend y envia un mensaje. Si `llm_source` empieza con `gemini_`, esa llamada uso Gemini y veras tokens/costo. Si empieza con `mock_fallback_gemini_`, Gemini fallo y esa llamada no tiene costo real calculable desde `usageMetadata`.

Los precios se pueden ajustar en `backend/.env`:

```txt
GEMINI_INPUT_PRICE_PER_MILLION=0.10
GEMINI_OUTPUT_PRICE_PER_MILLION=0.40
```

Normalmente no necesitas definir `GEMINI_URL`. Si la defines manualmente, debe contener `{model}` sin codificar:

```txt
GEMINI_URL=https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent
```

## Como saber que provider esta activo

Con el backend encendido:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/runtime
```

La interfaz tambien muestra `LLM activo` en la tarjeta superior.

Si el resultado muestra `provider: local` o `provider: mock`, revisa que `backend/.env` tenga el valor esperado y que FastAPI haya sido reiniciado.

## Rendimiento

El proyecto incluye cache en memoria para acelerar pruebas repetidas:

```txt
LLM_CACHE_ENABLED=true
LLM_CACHE_MAX_ITEMS=64
```

La cache no se guarda en disco y se pierde al reiniciar FastAPI.

Para una exposicion:

1. Usa `mock` si necesitas maxima velocidad y cero dependencias.
2. Usa `gemini` si quieres mostrar nube real.
3. Usa `local` si quieres defender privacidad y ejecucion offline.

El motor de decision no depende del LLM. Si el LLM falla, el backend vuelve al extractor simple para que la demo no quede inutilizable.
