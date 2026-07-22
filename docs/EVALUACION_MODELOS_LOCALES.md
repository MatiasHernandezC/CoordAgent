# Evaluacion de modelos locales

Fecha: 2026-07-08

## Objetivo

Probar los modelos GGUF locales contra casos dificiles de disponibilidad en espanol informal, usando el mismo prompt, parser semantico y compilador determinista que usa Coordina.

## Comando reproducible

Desde `coordinador-simple-mvp`:

```powershell
& "C:\Users\cocan\Downloads\(Ultimos ramos)\TAVI\.venv\Scripts\python.exe" tools\evaluate_local_models.py --models qwen phi qwen-mini tiny --max-tokens 500 --ctx 4096
```

El evaluador carga cada GGUF una sola vez, ejecuta los casos, compila el JSON con el backend real y escribe un reporte JSON en `data/local_model_eval.json` o en el archivo indicado con `--out`.

## Resultados

| Modelo | Resultado | Lectura |
|---|---:|---|
| `Qwen_Qwen3-4B-Instruct-2507-Q4_K_M.gguf` | 10/10 | Mejor candidato local. Entiende negaciones, topes, atribucion y casos indirectos. |
| `microsoft_Phi-4-mini-instruct-Q4_K_M.gguf` | 7/10 | Utilizable para casos simples, pero falla en semantica de turno, huecos y atribucion fina. |
| `qwen2.5-0.5b-instruct-q4_k_m.gguf` | 4/10 | Rapido, pero demasiado debil para agenda real. |
| `tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf` | 0/10 | No recomendado: copia el esquema o inventa campos. |

## Ajustes aplicados

- Se agrego `tools/evaluate_local_models.py` para repetir comparativas locales.
- Se agrego `tools/probe_local_architecture.py` para probar el flujo completo con
  modelo local real: LLM local -> parser -> compilador -> merge -> calculo.
- Se corrigio el grounding de primera persona para frases como `me acomoda`, `me sirve`, `me tinca`, `me va bien`, `vuelvo`, `regreso` y `llego`.
- Se corrigio la atribucion de frases con `y ... solo puede ...`, evitando que una restriccion de Luisa se aplique a Ana.
- Se agrego un override determinista para frases tipo `vuelvo de la clinica tipo 11, de ahi en adelante libre`, que algunos modelos locales interpretan al reves.
- Se amplio el override a tercera persona (`Pedro vuelve...`) y se protegieron
  remociones semanticas de `only/solo` para que no se pierdan en el grounding.
- `merge_extraction` ahora recalcula opciones y limpia decisiones obsoletas
  cuando cambia disponibilidad, pero conserva una decision confirmada si el
  extractor no trae datos nuevos.

## Validacion De Arquitectura Con Qwen Local

Comando:

```powershell
& "C:\Users\cocan\Downloads\(Ultimos ramos)\TAVI\.venv\Scripts\python.exe" tools\probe_local_architecture.py --model qwen --timeout 240 --out data\local_architecture_probe.json
```

Resultado de la ultima corrida:

| Escenario | Resultado | Tiempo local |
|---|---:|---:|
| `incremental_hard_group` | PASS | 265.24 s |
| `correction_after_confirmation` | PASS | 139.15 s |

Lectura: Qwen local sirve para validar arquitectura en esta maquina, pero es
lento en CPU. Para produccion en droplet conviene seguir usando modelo remoto y
mantener el local como herramienta de pruebas/desarrollo.

## Recomendacion

Para pruebas locales reales usar:

```env
LLM_PROVIDER=local
LOCAL_LLM_MODEL=qwen
LOCAL_LLM_TIMEOUT_SECONDS=240
LLM_FALLBACK_ENABLED=true
```

`qwen` es bastante mas lento que Gemini en CPU, pero fue el unico modelo local que paso la bateria completa. Mantener fallback activo sigue siendo recomendable para que una salida invalida no rompa la demo.
