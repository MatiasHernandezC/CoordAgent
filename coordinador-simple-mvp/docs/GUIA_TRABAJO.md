# Guia De Trabajo

Guia para continuar el proyecto sin perder la separacion de responsabilidades.

## Objetivo Actual

Construir y mantener un coordinador de horarios con:

1. Panel administrativo profesional.
2. Login real con roles y API protegida por FastAPI.
3. Bot de WhatsApp para grupos reales.
4. Una sesion automatica por grupo.
5. Extraccion de disponibilidad con Gemini y fallback por reglas.
6. Calculo determinista de mejores horarios.
7. Historial, sesiones, grupos abiertos y formato de respuesta configurable.

## Principio Tecnico

```txt
LLM/fallback = interpretar texto.
Decision engine = calcular opciones.
Session service = fusionar estado y persistir.
Gateway = transportar mensajes de WhatsApp.
Frontend = administrar y supervisar.
```

No mezclar estas capas salvo que haya una razon concreta.

## Areas De Trabajo

### Frontend

Archivos principales:

- `frontend/src/App.tsx`
- `frontend/src/api.ts`
- `frontend/src/types.ts`
- `frontend/src/styles.css`
- `frontend/src/components/`

Responsabilidades:

- Login y sesion de credenciales en `sessionStorage`.
- Lista de sesiones y filtros por grupo/manual/activo.
- Vista de historial de canal.
- Selector `text | image | both`.
- Panel de invitacion manual para agregar el bot al grupo.
- Estado de runtime: provider, modelo, cache, fallback y warnings.
- UX liviana: no agregar librerias pesadas sin necesidad.

### Backend

Archivos principales:

- `backend/app/main.py`
- `backend/app/bff/routes.py`
- `backend/app/schemas.py`
- `backend/app/services/llm_service.py`
- `backend/app/services/session_service.py`
- `backend/app/services/decision_engine.py`
- `backend/app/services/image_render.py`
- `backend/app/storage/postgres_repository.py`
- `backend/app/storage/json_repository.py`

Responsabilidades:

- Validar entrada de API.
- Extraer disponibilidad y remociones.
- Normalizar mensajes raros de WhatsApp.
- Aplicar removals sin destruir datos no relacionados.
- Calcular matriz y opciones.
- Renderizar imagen opcional.
- Persistir sesiones en Postgres o JSON.
- Mantener respuestas controladas ante errores del LLM.

### Gateway

Archivo principal:

- `gateway/src/index.js`

Responsabilidades:

- Conectar Baileys con credenciales persistidas en `waauth`.
- Escuchar solo grupos (`@g.us`).
- Ignorar mensajes del propio bot.
- Crear/sincronizar sesion por grupo.
- Recortar `sender` y `text` a los limites del backend.
- Enviar texto, imagen o ambos segun respuesta del backend.
- Loguear errores sin filtrar secretos.

### Infraestructura

Archivos principales:

- `docker-compose.prod.yml`
- `Caddyfile`
- `.env.prod.example`
- `backend/Dockerfile`
- `frontend/Dockerfile`
- `gateway/Dockerfile`

Responsabilidades:

- Caddy publica solo 80/443.
- Backend, DB y gateway quedan privados en red Docker.
- PostgreSQL usa volumen `pgdata`.
- WhatsApp usa volumen `waauth`.
- El primer administrador y el secreto interno del gateway se configuran por entorno.
- Produccion usa Gemini remoto para no cargar RAM local.

## Flujo De Desarrollo Recomendado

1. Reproducir el caso con una prueba o smoke local.
2. Modificar la capa correcta.
3. Agregar prueba si el cambio toca logica, parsing, API o gateway.
4. Correr validaciones.
5. Desplegar solo el servicio necesario.
6. Probar por HTTPS en produccion.
7. Actualizar docs si cambia comportamiento operativo.

## Comandos De Validacion

Backend:

```powershell
$env:PYTHONPATH="backend"
backend\.venv\Scripts\python.exe -m pytest backend\tests -q
backend\.venv\Scripts\python.exe -m py_compile backend\app\services\llm_service.py
```

Gateway:

```powershell
node --check gateway\src\index.js
```

Frontend:

```powershell
npm --prefix frontend run build
```

Produccion:

```bash
cd /opt/coordinador-simple-mvp
docker compose -f docker-compose.prod.yml --env-file .env.prod ps
docker stats --no-stream
```

## Casos Que Siempre Hay Que Probar

Mensajes normales:

```txt
yo puedo lunes en la tarde
Camila puede martes desde las 16
Pedro puede cualquier hora todos los dias
```

Mensajes raros o informales:

```txt
estoy libre lun de 3 a 5 pm
me sirve mierc 15:30-17:00
no me va bien martes de 10 a 12
yo no podre nigun dia
@coordina jajaj asdf $$$ 123
```

Canal:

```txt
@coordina
@coordina solo texto
@coordina con imagen
@coordina solo imagen
```

Seguridad:

```txt
/api/runtime sin auth -> 401
/.env -> 404
/health con auth -> ok=true
```

## Que Evitar

- No guardar secretos en README, docs, commits o screenshots.
- No exponer puertos de backend/Postgres/gateway.
- No meter logica de calculo dentro del prompt.
- No depender solo de Gemini para casos basicos.
- No agregar dependencias pesadas al frontend sin beneficio claro.
- No borrar volumen `waauth` salvo que quieras reescanear QR.
- No borrar volumen `pgdata` salvo backup y decision explicita.

## Roadmap Realista

1. Mejorar observabilidad del gateway: contadores por grupo y ultimo error.
2. Agregar auditoria de acciones del administrador.
3. Agregar rate limiting.
4. Agregar limpieza/archivo de sesiones antiguas.
5. Mejorar gestion de usuarios si hay mas administradores.
6. Separar staging de produccion.
7. Evaluar modelo alternativo solo con comparacion de calidad/costo.
