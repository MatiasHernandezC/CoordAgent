# Guia de trabajo para 2 desarrolladores

## Objetivo del MVP

Construir una app simple para coordinar reuniones:

1. Crear una sesion.
2. Escribir disponibilidad en texto natural.
3. Usar un LLM para extraer participantes y horarios.
4. Calcular los mejores horarios con Python.
5. Mostrar cobertura, faltantes y explicacion.
6. Corregir datos manualmente si el LLM no entiende algo.
7. Confirmar una opcion.

## Division recomendada

### Desarrollador 1: Frontend + experiencia

Responsable de:

- Pantalla principal React.
- Formulario para crear sesion.
- Caja para escribir mensaje.
- Vista de participantes detectados.
- Metricas de participantes, disponibilidades, faltantes y cobertura.
- Mapa de disponibilidad por dia/hora.
- Historial de interpretacion del LLM.
- Lista de opciones sugeridas con explicacion.
- Correccion manual de horarios.
- Boton confirmar.
- Manejo visual de errores/carga.

Archivos principales:

- `frontend/src/App.tsx`
- `frontend/src/api.ts`
- `frontend/src/types.ts`
- `frontend/src/styles.css`

### Desarrollador 2: Backend + logica

Responsable de:

- FastAPI.
- Endpoints.
- Servicio LLM.
- Repositorio JSON.
- Motor de decision.
- Explicaciones de opciones.
- Matriz de disponibilidad.
- Historial de mensajes.
- Resumen final.
- Tests del motor.

Archivos principales:

- `backend/app/main.py`
- `backend/app/bff/routes.py`
- `backend/app/services/llm_service.py`
- `backend/app/services/session_service.py`
- `backend/app/services/decision_engine.py`
- `backend/app/storage/postgres_repository.py` y `backend/app/storage/json_repository.py`

## Flujo de trabajo sugerido

1. Levantar backend.
2. Probar `/health`.
3. Crear sesion desde el frontend.
4. Enviar mensaje como:

```txt
Yo puedo lunes en la tarde, Camila puede lunes desde las 16 y Diego puede martes en la manana, Pedro puede a cualquier hora todos los dias.
```

5. Revisar que aparezcan participantes.
6. Corregir manualmente si falta informacion.
7. Calcular opciones.
8. Revisar el mapa de disponibilidad.
9. Revisar cobertura y participantes que no calzan.
10. Confirmar una opcion.

## Que no hacer en este MVP

- Login.
- Google Calendar real.
- WhatsApp real.
- OAuth.
- Multiempresa.
- Dashboard.
- Prompt injection avanzado.
- Streaming.

## Regla tecnica importante

```txt
LLM = interpreta texto y devuelve JSON.
Python = calcula la mejor opcion.
Usuario = confirma.
```

Si mantienen esa separacion, el proyecto sigue siendo defendible.
