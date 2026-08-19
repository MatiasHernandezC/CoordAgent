# Operacion De Produccion

## Salud

Docker valida cinco capas:

- PostgreSQL acepta conexiones.
- FastAPI ejecuta `SELECT 1` mediante `/ready`.
- nginx entrega el frontend.
- el gateway confirma una conexion abierta con WhatsApp.
- Caddy mantiene cargada una configuracion valida.

El endpoint autenticado `/api/ops/status` permite que el panel muestre el estado
real del gateway. `Vinculado` solo aparece cuando Baileys recibio
`connection=open`; un socket creado pero aun conectando no cuenta como listo.

Los mensajes entrantes se guardan antes de procesarlos en
`waauth/pending-messages.json`. Si FastAPI, Gemini o WhatsApp se cortan, el
gateway reintenta con backoff. Cada request lleva el ID original de WhatsApp y
FastAPI reproduce el resultado ya guardado en vez de confirmar o extraer dos
veces. La entrega es al menos una vez: un corte en el instante posterior al
envio podria repetir una respuesta, pero ya no pierde silenciosamente la
solicitud.

## Monitor Del Droplet

`ops/check_production_health.sh` agrega comprobaciones de Compose, consulta SQL,
frontend, Caddy, HTTPS, proteccion de la API, WhatsApp, respaldo cifrado y disco.
No envia mensajes ni reinicia servicios. Una reconexion o un fallo HTTPS externo
solo se declara fallo tras tres ejecuciones consecutivas; `logged_out`, una base
inaccesible o una API desprotegida fallan de inmediato.

Instalacion:

```bash
install -m 0755 ops/check_production_health.sh /usr/local/sbin/tavi-coordina-health
install -m 0644 ops/tavi-coordina-health.service /etc/systemd/system/
install -m 0644 ops/tavi-coordina-health.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now tavi-coordina-health.timer
systemctl start tavi-coordina-health.service
```

Revision:

```bash
systemctl status tavi-coordina-health.service --no-pager
systemctl list-timers tavi-coordina-health.timer --no-pager
journalctl -u tavi-coordina-health.service -n 100 --no-pager
cat /var/lib/tavi-coordina-monitor/last-result.json
```

El resultado `degraded` es transitorio y no falla el servicio hasta acumular tres
lecturas. `failed` requiere intervencion. Los logs de cada contenedor rotan a
cinco archivos de 10 MB para impedir crecimiento ilimitado.

## Prueba E2E Sin WhatsApp Real

`ops/smoke_api.py` recorre por HTTP la creacion, disponibilidad, propuesta,
confirmacion, adjunto `.ics`, resumen, cancelacion y archivo. Tiene un seguro que
rechaza hosts que no sean loopback. Debe ejecutarse contra un backend temporal
con `DB_BACKEND=json` y `LLM_PROVIDER=mock`, nunca contra los grupos reales.

```bash
docker run --rm -d --name tavi-coordina-smoke \
  -p 127.0.0.1:18000:8000 \
  -e DB_BACKEND=json -e DATA_FILE=/tmp/sessions.json -e LLM_PROVIDER=mock \
  coordinador-simple-mvp-backend
python3 ops/smoke_api.py --base-url http://127.0.0.1:18000
docker stop tavi-coordina-smoke
```
