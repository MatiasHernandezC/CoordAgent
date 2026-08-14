# Integracion con Google Calendar (OAuth)

Ademas del link "Agregar a Google Calendar" y el `.ics` de siempre, si el
administrador conecta una cuenta de Google, el evento se crea de verdad ahi
via la API de Calendar (el link y el `.ics` siguen mandandose igual, como
respaldo).

Es una conexion a nivel administrador/organizacion, no una por participante:
una sola cuenta de Google autoriza a Coordina. El refresh token se cifra con
la misma llave maestra de las llaves Gemini (`LLM_KEYS_MASTER_KEY`) y nunca
se expone por la API (`backend/app/services/google_calendar_service.py`).

## 1. Proyecto y credenciales OAuth en Google Cloud

1. Entra a <https://console.cloud.google.com/> y crea un proyecto (o usa uno
   existente). Nombre sugerido: `coordina-tavi`.
2. **APIs & Services -> Library**: busca **Google Calendar API** y habilitala.
3. **APIs & Services -> OAuth consent screen**:
   - **User Type**: External.
   - Nombre de la app: `Coordina`. Correo de soporte: el tuyo.
   - **Scopes**: agrega `https://www.googleapis.com/auth/calendar.events`.
   - **Test users**: agrega los correos Gmail que van a conectar la cuenta
     (por ejemplo el del administrador del grupo). En modo Testing, solo
     esos correos pueden autorizar, sin pasar por revision de Google.
4. **APIs & Services -> Credentials -> Create Credentials -> OAuth client ID**:
   - Tipo: **Web application**.
   - **Authorized redirect URIs**:
     - Local: `http://127.0.0.1:8000/api/admin/google-calendar/callback`
     - Produccion: `https://coordina.xshift007.com/api/admin/google-calendar/callback`
   - Copia **Client ID** y **Client secret**.

## 2. Variables de entorno

En `backend/.env` (local) o `.env.prod` (droplet):

```txt
GOOGLE_CLIENT_ID=...apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=...
GOOGLE_OAUTH_REDIRECT_URI=http://127.0.0.1:8000/api/admin/google-calendar/callback
```

En produccion, la URL debe coincidir exacto con el redirect URI de Google
Cloud (incluyendo `https://`).

Tambien hace falta `LLM_KEYS_MASTER_KEY` (32 bytes en base64) — es la misma
variable que usan las llaves Gemini, no hay que duplicarla:

```powershell
python -c "import secrets,base64; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"
```

Sin `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET`/`GOOGLE_OAUTH_REDIRECT_URI` el
panel muestra la tarjeta como "no configurada" y sigue todo igual que antes.

## 3. Conectar la cuenta desde el panel

1. Panel de Coordina ya logueado, tarjeta **Google Calendar** -> **Conectar
   Google Calendar**. Abre una pestana con el consentimiento de Google.
2. Elige la cuenta (tiene que ser una de las agregadas como test user) y
   acepta los permisos.
3. Google redirige a `/api/admin/google-calendar/callback`, que guarda el
   refresh token cifrado. Cierra esa pestana y vuelve al panel.
4. La tarjeta muestra **Cuenta conectada** con el correo (se actualiza solo
   cada ~20s, o dale a "Actualizar panel").

## 4. Probar

Confirma cualquier decision (panel o `@coordina confirmar` por
WhatsApp/Slack). La respuesta debe decir "Evento creado en Google Calendar"
con el link real, en vez del link generico de siempre. Revisa el calendario
de la cuenta: el evento debe salir con el titulo del grupo y los asistentes
en la descripcion.

## Seguridad y limites

- El `state` de la autorizacion es de un solo uso y expira a los 10 minutos
  — evita reusar un link de autorizacion capturado.
- El callback de Google llega como redireccion del navegador del propio
  administrador, que ya tiene Basic Auth cacheado para ese dominio.
- Desconectar desde el panel borra el refresh token local; no revoca el
  acceso en Google. Para eso hay que ir a
  <https://myaccount.google.com/permissions> y quitarlo ahi.
- Si Google no devuelve `refresh_token` (pasa cuando la cuenta ya habia
  autorizado antes), el callback pide revocar el acceso previo en
  `myaccount.google.com/permissions` y reconectar.