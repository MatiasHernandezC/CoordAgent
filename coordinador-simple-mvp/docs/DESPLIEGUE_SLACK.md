# Canal Slack

Coordina puede escuchar canales de Slack ademas de grupos de WhatsApp, usando
el mismo pipeline (RAG estructurado, extraccion LLM, motor de decision,
guardrails). Lo nuevo es solo el traductor de eventos:
`backend/app/bff/slack_routes.py` + `backend/app/services/slack_channel.py`.

La invocacion funciona con `@coordina` como texto plano o como mencion real
del bot (Slack la autocompleta apenas escribes el nombre); ambas formas
terminan disparando la coordinacion.

## 1. Crear el Slack App

1. Entra a <https://api.slack.com/apps> con la cuenta del workspace de prueba
   (sirve uno personal/gratuito).
2. **Create New App -> From scratch**. Nombre sugerido: `Coordina`. Elige el
   workspace donde vas a probar.
3. En **OAuth & Permissions**:
   - **Scopes -> Bot Token Scopes**, agrega:
     - `chat:write` (enviar mensajes)
     - `files:write` (subir la imagen del calendario y el `.ics`)
     - `channels:read` (nombre del canal para el panel)
     - `users:read` (nombre visible de quien escribe)
   - **Install to Workspace**, acepta los permisos.
   - Copia el **Bot User OAuth Token** (`xoxb-...`) -> `SLACK_BOT_TOKEN`.
4. En **Basic Information -> App Credentials**, copia **Signing Secret** ->
   `SLACK_SIGNING_SECRET`.
5. Invita el bot a un canal de prueba: `/invite @Coordina`.
6. En **Socket Mode**, dejalo **desactivado**. Si queda prendido, Slack manda
   los eventos por websocket en vez de pegarle a la Request URL y el webhook
   nunca recibe nada (aunque la URL aparezca "Verified").

## 2. Variables de entorno

En `backend/.env` (local) o `.env.prod` (droplet):

```txt
SLACK_BOT_TOKEN=xoxb-...
SLACK_SIGNING_SECRET=...
SLACK_TRIGGER_WORD=@coordina
```

Sin estas dos variables el endpoint responde `503` y el resto de la app
sigue igual. Reinicia el backend despues de cambiar `.env`.

## 3. Activar el Events API (necesita URL publica)

Necesitas HTTPS publico: el droplet con Caddy, o un tunel (`cloudflared` sin
cuenta, o `ngrok`) apuntando a tu backend local en el puerto 8000 mientras
pruebas.

1. En el Slack App, **Event Subscriptions -> Enable Events**.
2. **Request URL**: `https://TU_DOMINIO/api/channels/slack/events`. Slack
   manda un `url_verification` al tiro y se valida solo si el backend esta
   arriba y `SLACK_SIGNING_SECRET` esta puesto.
3. **Subscribe to bot events**, agrega `message.channels`. Para canales
   privados tambien agrega `message.groups` + scopes `groups:history`/`groups:read`.
4. Guarda. Si Slack pide reinstalar el app (banner amarillo), hazlo.

En produccion, `/api/channels/slack/events` no usa una cuenta del panel: la
autenticacion real es la firma `X-Slack-Signature`, verificada en cada request.

## 4. Botones para confirmar (Interactivity & Shortcuts)

Ademas de escribir `@coordina confirmar N`, el canal de Slack muestra un
boton por cada opcion propuesta. Para activarlos:

1. En el Slack App, **Interactivity & Shortcuts -> Activar**.
2. **Request URL**: `https://TU_DOMINIO/api/channels/slack/interactions`.
   No hace falta un evento aparte: Slack manda el click aca directo (payload
   `block_actions`, verificado con la misma firma `X-Slack-Signature`).
3. Guarda. Si pide reinstalar el app, hazlo (igual que con Events API).

Al tocar un boton, el mensaje original se reemplaza en el mismo lugar con la
confirmacion (no queda un mensaje duplicado). El `.ics` se sube aparte, igual
que con `@coordina confirmar` por texto.

En produccion, Caddy tambien deja `/api/channels/slack/interactions` sin
Basic Auth por el mismo motivo que `/events` (ver `Caddyfile`).

## 5. Probar

En el canal donde invitaste al bot:

```txt
Camila puede lunes en la tarde
@coordina
```

Responde con las opciones (texto + botones), mismo formato que WhatsApp. Para
confirmar: `@coordina confirmar` o tocando el boton de la opcion.

Si algo falla, mira los logs del backend (`slack_event_processing_failed`,
`slack_delivery_failed`, `slack_block_action_failed`). El handler nunca
revienta el webhook — Slack siempre recibe `200 OK` y el error queda solo en
el log.

## Limitaciones (MVP)

- No hay padron de canal como en WhatsApp: el panel no muestra cuantos
  integrantes tiene el canal de Slack.
- Comandos que requieren coordinador (`hazlo requerido`, etc.) no distinguen
  admins de Slack todavia: `coordinator_ids` queda vacio al resolver el canal.
- Solo canales (`channel`/`group`), no mensajes directos (`im`).
