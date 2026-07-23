# Validacion y endurecimiento RAG estructurado

Documento vivo del comportamiento **real** (local + droplet).

## Estado

```txt
ENDURECIMIENTO COMPLETADO
```

con limitaciones residuales honestas al final.

---

## Principio

```txt
LLM puede interpretar.
Python debe validar.
La ambigüedad debe permanecer ambigua.
```

Capas de información (no se confunden):

| Capa | Uso permitido |
|------|----------------|
| **Texto actual** | Grounding principal de días y personas |
| **Contexto activo de ronda** | Solo lenguaje de coordinación explícito (“coordinando para el jueves”) |
| **Memoria histórica (RAG)** | Desambiguar identidad / continuidad; **nunca** day grounding |
| **Inferencia del modelo** | Siempre filtrada por Python |

---

## Grounding temporal

### Bug corregido

`discard_ungrounded_days` antes hacía:

```python
if not allowed_days:
    return extraction  # conservaba días inventados por el LLM
```

### Comportamiento actual

Módulo `backend/app/services/temporal_grounding.py`:

1. Días del **mensaje** (weekday, relativos `hoy/mañana`, rangos cruzados).
2. Días de **ronda activa** (patrones de coordinación, no narrativa histórica).
3. Si un slot del LLM no está en (1∪2) → se **rechaza**.
4. Si no hay ningún día grounded → se vacían slots de día y se marcan flags:
   - `rejected_model_day_inference`
   - `temporal_day_ungrounded`
5. Decisiones RAG / `decision_history` **no** aportan días permitidos.

Logs sanitizados:

```txt
temporal_grounding source=rejected_model_inference rejected_days=['lunes'] ...
```

### Evidencia droplet (Gemini)

| Caso | Resultado |
|------|-----------|
| A `Puedo despues de las 18.` | `days set()` — sin día inventado |
| B ronda activa jueves + `Yo despues de las 18` | acepta **jueves** |
| C histórico martes + solo viernes en texto | `days {'viernes'}` + flag rejected |

---

## Identidad y alias

Precedencia real:

```txt
1) ID de canal confiable (normalize_channel_extraction)
2) Alias único conocido / nombre exacto del roster (apply_memory_identity)
3) Ambigüedad o desconocido (se deja el label original)
```

Reglas:

- Match **exacto** normalizado (minúsculas, tildes, `@` strip).
- **Sin** fuzzy / parcial (`Nicol` ≠ `Nicolas` / `Nicole`).
- Alias duplicado (dos personas con mismo apodo) → no se resuelve; flag `ambiguous_alias_identity`.
- Filas con `external_id` no se renombran por memoria textual.

Evidencia:

- `Yuli` → `Julissa (Yuli)` (Gemini droplet).
- `Cata` desconocida permanece `Cata`.

---

## Parsing Gemini

Orden:

1. Solicitud normal (`responseMimeType=application/json`).
2. `extract_json` (fences markdown, objeto balanceado, validación `json.loads`).
3. Validación schema / compile.
4. **Un** reintento correctivo con prompt de reparación.
5. Si falla → error tipado → fallback de reglas del caller.
6. Sin loops.

Logs:

```txt
gemini_json_parse_error provider=gemini retry=1 error_type=...
gemini_json_parse_recovered ...
gemini_json_parse_failed ... fallback=rules
```

Tests: fenced JSON, retry único, fallo seguro, payload semánticamente inválido.

---

## Tests locales

```powershell
cd backend
$env:PYTHONPATH="."; $env:DB_BACKEND="json"; $env:LLM_PROVIDER="mock"
.\.venv\Scripts\python.exe -m pytest tests/test_temporal_grounding.py tests/test_identity_hardening.py tests/test_gemini_json_hardening.py tests/test_group_memory.py -q
# 37 passed (cierre congelado)

.\.venv\Scripts\python.exe -m pytest -q
# 291 passed (cierre congelado)
```

---

## Guion de demo (3–5 min)

Escenario ficticio en panel (preferible) o grupo de prueba.

1. **Setup (30 s)**  
   Crear sesion `Demo RAG`. Participantes: `Nicolas`, `Julissa (Yuli)`, `Matias`.

2. **Memoria / RAG (45 s)**  
   Enviar: `Nicolas puede jueves de 15 a 18 y Julissa puede jueves de 15 a 18`.  
   Mostrar en card Procesamiento: `retrieval_used` / preview con roster (si hay ronda previa, tambien decisiones).

3. **Alias (45 s)**  
   Enviar: `Yuli puede el viernes de 16 a 18`.  
   Mostrar extraccion con nombre canonico `Julissa (Yuli)` (no duplicar Yuli suelto).

4. **Grounding temporal (60 s)**  
   Enviar: `Puedo despues de las 18.`  
   Explicar: **no se inventa un dia**; flags de rechazo o disponibilidad parcial.  
   Contraste: con contexto de ronda `Estamos coordinando para el jueves` + `Yo despues de las 15` el jueves si es valido.

5. **Motor Python (45 s)**  
   Mostrar opciones con % de cobertura.  
   Cierre: “Gemini interpreta; Python decide el horario”.

Narrativa en una frase:

```txt
RAG estructurado recupera memoria de sesion/grupo;
Gemini extrae JSON; Python valida dias/identidad y calcula opciones.
No hay base vectorial.
```

Scripts droplet (sin redeploy): `ops/remote_harden_smoke.sh` (si el codigo endurecido ya esta desplegado).

---

## Smoke Gemini en droplet

Host: `161.35.17.179` `/opt/coordinador-simple-mvp`

| Caso | Resultado sanitizado |
|------|----------------------|
| A hora sin día | rejected_model_inference; live days vacío |
| B ronda activa | active_round → jueves |
| C histórico vs viernes | solo viernes; flag rejected |
| D alias Yuli | people `['Julissa (Yuli)']` |
| E alias desconocido | permanece Cata |
| F JSON fenced | parse OK |

Post-deploy:

- backend/frontend/gateway/db/caddy: **healthy**
- gateway no rebuild
- CPU/RAM normales (~65–97 MiB backend/gateway)

Backup / rollback:

```txt
.deploy-backup/harden-20260722T201310Z/
imagen: coordinador-simple-mvp-backend:pre-harden-*
```

---

## Limitaciones reales restantes

1. **Sin día ni ronda activa**, el sistema no inventa día, pero tampoco “adivina” intención; la ronda puede quedar sin opciones hasta aclarar el día.
2. **Ronda activa** solo por patrones de coordinación explícitos o `active_round_days`; no hay campo de UI dedicado aún.
3. **Sin staging** separado: validación Gemini en droplet productivo del MVP, con backups.
4. Alias solo roster/parentesis/hints/id; no diccionario libre de apodos.
5. Reintento JSON = 1; si el modelo devuelve basura dos veces, cae a reglas/fallback.
6. Git WIP mezclado: **sin limpieza** en esta fase.

---

## Push / Git

- Push: **no**
- Limpieza final / commit definitivo: **pendiente** a indicación del usuario
