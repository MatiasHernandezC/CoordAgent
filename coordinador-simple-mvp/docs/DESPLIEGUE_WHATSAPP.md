# Despliegue en un droplet con WhatsApp real

Integra un grupo de WhatsApp real con el backend, usando un *gateway* basado en
[Baileys](https://github.com/WhiskeySockets/Baileys). El agente vive como una
cuenta de WhatsApp dentro del grupo y responde cuando alguien escribe la palabra
de invocacion (por defecto `@coordina`).

> **Aviso importante:** Baileys automatiza WhatsApp Web de forma **no oficial**,
> lo que va contra los Terminos de Servicio de WhatsApp. Usa un **numero
> dedicado o desechable**, nunca tu numero personal. Es apto para demos y
> prototipos, no para produccion a gran escala. La API oficial (WhatsApp Cloud
> API) **no soporta grupos**, por eso este camino usa la via no oficial.

## Arquitectura

```
Grupo de WhatsApp  <->  gateway (Baileys)  ->  backend (FastAPI)  ->  PostgreSQL
                                                     ^
                                                     |
                                              Gemini API (extraccion)
Navegador (panel)  ->  Caddy (HTTPS)  ->  frontend / backend
```

El gateway reutiliza los endpoints de canal que ya existen en el backend
(`/channel/messages`), asi que la logica de decision no cambia.

## Requisitos

- Un droplet (Ubuntu) con **Docker** y **Docker Compose** instalados.
- Un **numero de telefono dedicado** con WhatsApp instalado (para escanear el QR).
- Una **API key de Gemini** (o usa `LLM_PROVIDER=mock` para probar sin IA).
- Opcional pero recomendado: un **dominio** apuntando al droplet (para HTTPS).
- Recursos: **1-2 GB de RAM** bastan (Baileys es liviano y el LLM corre en la nube).

## Pasos

### 1. Subir el proyecto al droplet

Copia la carpeta `coordinador-simple-mvp/` al droplet (con `scp`, `rsync` o `git`).

### 2. Crear el archivo de entorno

```bash
cd coordinador-simple-mvp
cp .env.prod.example .env.prod
nano .env.prod   # completa dominio, clave de Postgres y GEMINI_API_KEY
```

### 3. Apuntar el dominio (si usas HTTPS)

Crea un registro DNS `A` de tu subdominio hacia la IP del droplet, y abre los
puertos 80 y 443 en el firewall del droplet. Caddy sacara el certificado solo.

Para una prueba rapida sin dominio, deja `PUBLIC_DOMAIN=localhost` y
`PUBLIC_URL=http://localhost` (Caddy servira por http en el puerto 80).

### 4. Construir y levantar

```bash
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d --build
```

Esto levanta db, backend, frontend, gateway y Caddy. Todos con
`restart: unless-stopped`, asi que sobreviven reinicios del droplet.

### 5. Vincular WhatsApp (una sola vez)

Mira los logs del gateway para ver el QR:

```bash
docker compose -f docker-compose.prod.yml logs -f gateway
```

En el telefono del numero dedicado: **WhatsApp -> Dispositivos vinculados ->
Vincular un dispositivo** y escanea el QR. El log dira "Gateway conectado".
La sesion queda guardada en el volumen `waauth`, asi que **no hay que reescanear**
en cada reinicio.

### 6. Probar

1. Agrega el numero dedicado a un grupo de WhatsApp.
2. Los participantes escriben su disponibilidad, por ejemplo:
   - "yo puedo lunes en la tarde"
   - "yo puedo martes en la manana"
3. Alguien invoca al agente: "@coordina nos ayudas a cerrar horario?"
4. El agente responde en el grupo con la mejor opcion y su cobertura.

El panel web (para revisar sesiones) queda en `https://tu-dominio/`.

## Mantenerlo corriendo

- `restart: unless-stopped` reinicia los servicios si se caen o si el droplet se reinicia.
- Datos persistentes en volumenes Docker: `pgdata` (base) y `waauth` (sesion de WhatsApp).
- Ver estado: `docker compose -f docker-compose.prod.yml ps`
- Ver logs: `docker compose -f docker-compose.prod.yml logs -f backend gateway`
- Actualizar: `git pull` (o re-subir) y `docker compose -f docker-compose.prod.yml up -d --build`

## Seguridad

- La base **no expone puerto** al exterior (solo la red interna de Docker).
- Solo Caddy publica 80/443. Nada de 8000 ni 5432 abiertos.
- Pon una `POSTGRES_PASSWORD` fuerte y no subas `.env.prod` al repo.
- La app **no tiene autenticacion**: si el panel web va a ser publico, protégelo
  (Cloudflare Access, basic auth en Caddy, o restriccion por IP).

## Problemas comunes

- **No aparece el QR**: revisa `docker compose ... logs -f gateway`. Si dice
  "sesion cerrada", borra el volumen y reescanea:
  `docker compose -f docker-compose.prod.yml down` y luego
  `docker volume rm coordinador-simple-mvp_waauth`, y vuelve a `up`.
- **El agente no responde**: confirma que el mensaje contiene la palabra de
  invocacion (`@coordina`) y que el numero del bot esta en el grupo.
- **429 de Gemini**: el backend entra en cooldown y usa `mock` si el fallback
  esta activo. Baja la frecuencia o revisa tu cuota.
- **Numero baneado**: riesgo inherente de la via no oficial. Usa numero desechable.
