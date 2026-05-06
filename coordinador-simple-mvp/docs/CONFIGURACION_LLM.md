# Configuracion de LLM

El backend decide que modelo usar mediante `backend/.env`.

Si `backend/.env` no existe, el proyecto usa los valores por defecto definidos en `backend/app/settings.py`. Para evitar confusiones, crea el archivo copiando `backend/.env.example`.

Importante: edita `backend/.env`, no `backend/.env.example`. Despues de cambiar el provider, reinicia FastAPI.

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
GEMINI_MODEL=gemini-2.0-flash
```

El backend no expone la API key al frontend. Solo informa si Gemini esta configurado o no.

No uses `LOCAL_LLM_MODEL=gemini` para seleccionar Gemini. Esa variable solo sirve cuando `LLM_PROVIDER=local`.

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
