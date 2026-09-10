# Respaldo Y Recuperacion De Produccion

Este procedimiento protege los dos estados que no se pueden reconstruir solo
desde Git:

- PostgreSQL (`pgdata`): sesiones, participantes, mensajes y decisiones.
- Baileys (`waauth`): credenciales vinculadas y mapa grupo-sesion.

Los respaldos nuevos **no incluyen `.env.prod`**. Se cifran en el droplet con un
certificado publico; la clave privada permanece fuera del servidor.

## Componentes

- `ops/backup_production.sh`: crea y valida el respaldo en el droplet.
- `ops/pull_encrypted_backup.ps1`: genera una copia, la descarga y la verifica.
- `ops/verify_encrypted_backup.ps1`: comprueba SHA256, descifrado y estructura.

Ruta esperada en el droplet:

```text
/usr/local/sbin/tavi-coordina-backup
/root/.config/tavi-backup/recipient.pem
/var/backups/tavi-coordina/
```

El timer `tavi-coordina-backup.timer` crea una copia diaria, recupera una
ejecucion perdida al volver a encender el servidor y conserva 14 dias en el
droplet. La tarea de Windows descarga una copia nueva al notebook cuando este se
encuentra disponible.

La clave privada local esperada es:

```text
%USERPROFILE%\.tavi-coordina-backup\private-key.pem
```

No debe copiarse al repositorio ni al droplet. Conserva una segunda copia en un
medio seguro separado; sin esa clave no es posible recuperar los `.cms`.

## Ejecucion Manual

Desde PowerShell:

```powershell
cd "<RUTA_PROYECTO>\coordinador-simple-mvp"
.\ops\pull_encrypted_backup.ps1 -SshHost root@<IP_DROPLET>
```

El script remoto valida `pg_dump` con `pg_restore --list`, valida el tar de
`waauth`, cifra el paquete y publica un SHA256. El script local vuelve a validar
el hash, el descifrado y los archivos obligatorios.

## Recuperacion

No restaures directamente sobre produccion sin guardar primero el estado actual.
Descifra y ensaya la recuperacion en un PostgreSQL temporal:

```powershell
openssl cms -decrypt -binary -inform DER `
  -in .\tavi-coordina-FECHA.cms `
  -recip "$HOME\.tavi-coordina-backup\recipient-cert.pem" `
  -inkey "$HOME\.tavi-coordina-backup\private-key.pem" `
  -out .\tavi-recuperado.tar.gz

tar -xzf .\tavi-recuperado.tar.gz -C .\tavi-recuperado
```

Antes de una restauracion real hay que:

1. Confirmar que `payload.sha256` coincide.
2. Cargar `postgres.dump` en una base temporal con `pg_restore`.
3. Validar sesiones y conteos.
4. Detener escrituras del gateway durante la restauracion definitiva.
5. Restaurar `waauth.tar.gz` solamente si es necesario recuperar el vinculo.
6. Levantar servicios y ejecutar `/health` y una prueba controlada de WhatsApp.
