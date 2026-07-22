#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

BACKUP_DIR="${BACKUP_DIR:-/var/backups/tavi-coordina}"
RECIPIENT_CERT="${RECIPIENT_CERT:-/root/.config/tavi-backup/recipient.pem}"
DB_CONTAINER="${DB_CONTAINER:-coordinador-simple-mvp-db-1}"
WAUTH_VOLUME="${WAUTH_VOLUME:-coordinador-simple-mvp_waauth}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"
LOCK_FILE="${LOCK_FILE:-/run/lock/tavi-coordina-backup.lock}"

require_command() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Falta el comando requerido: $1" >&2
    exit 1
  }
}

for command_name in docker tar openssl sha256sum flock find mktemp; do
  require_command "$command_name"
done

[[ -r "$RECIPIENT_CERT" ]] || {
  echo "No se puede leer el certificado de respaldo: $RECIPIENT_CERT" >&2
  exit 1
}

mkdir -p "$BACKUP_DIR" "$(dirname "$LOCK_FILE")"
chmod 700 "$BACKUP_DIR"

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "Ya existe un respaldo en ejecucion; no se inicia otro."
  exit 0
fi

docker inspect "$DB_CONTAINER" >/dev/null
wauth_path="$(docker volume inspect "$WAUTH_VOLUME" --format '{{.Mountpoint}}')"
[[ -d "$wauth_path" ]] || {
  echo "No existe el volumen WhatsApp esperado: $WAUTH_VOLUME" >&2
  exit 1
}

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup_name="tavi-coordina-${timestamp}"
staging_dir="$(mktemp -d "${BACKUP_DIR}/.staging-${timestamp}-XXXXXX")"
encrypted_tmp="${BACKUP_DIR}/.${backup_name}.cms.tmp"
encrypted_file="${BACKUP_DIR}/${backup_name}.cms"
checksum_file="${encrypted_file}.sha256"

cleanup() {
  rm -rf -- "$staging_dir"
  rm -f -- "$encrypted_tmp"
}
trap cleanup EXIT

docker exec "$DB_CONTAINER" sh -lc \
  'exec pg_dump --format=custom --no-owner --no-privileges --username="$POSTGRES_USER" --dbname="$POSTGRES_DB"' \
  >"${staging_dir}/postgres.dump"
test -s "${staging_dir}/postgres.dump"

# pg_restore --list valida la estructura del dump sin modificar la base.
docker exec -i "$DB_CONTAINER" pg_restore --list \
  <"${staging_dir}/postgres.dump" \
  >"${staging_dir}/pg_restore.list"
test -s "${staging_dir}/pg_restore.list"

# Baileys persiste credenciales como archivos JSON en este volumen. El tar se
# toma en lectura y se valida antes de cifrarlo; el gateway sigue ejecutandose.
tar -C "$wauth_path" -czf "${staging_dir}/waauth.tar.gz" .
tar -tzf "${staging_dir}/waauth.tar.gz" >/dev/null
tar -tzf "${staging_dir}/waauth.tar.gz" | grep -qE '(^|/)creds\.json$'

(
  cd "$staging_dir"
  sha256sum postgres.dump waauth.tar.gz >payload.sha256
)

cat >"${staging_dir}/metadata.txt" <<EOF
created_utc=${timestamp}
host=$(hostname)
database_container=${DB_CONTAINER}
waauth_volume=${WAUTH_VOLUME}
format=postgres-custom-plus-waauth-targz
encryption=openssl-cms-aes-256-cbc
EOF

# No se crea un tar completo sin cifrar en disco. El flujo tar.gz se entrega
# directamente a CMS y solo el archivo cifrado termina en BACKUP_DIR.
tar -C "$staging_dir" -czf - \
  postgres.dump pg_restore.list waauth.tar.gz payload.sha256 metadata.txt \
  | openssl cms -encrypt -binary -outform DER -aes-256-cbc \
      -recip "$RECIPIENT_CERT" -out "$encrypted_tmp"

test -s "$encrypted_tmp"
openssl cms -cmsout -inform DER -in "$encrypted_tmp" -noout
mv -f -- "$encrypted_tmp" "$encrypted_file"
(
  cd "$BACKUP_DIR"
  sha256sum "$(basename "$encrypted_file")" >"$(basename "$checksum_file")"
)

# La retencion solo afecta respaldos creados por este script. Los tar historicos
# del usuario y cualquier otro archivo del servidor quedan intactos.
find "$BACKUP_DIR" -maxdepth 1 -type f \
  \( -name 'tavi-coordina-*.cms' -o -name 'tavi-coordina-*.cms.sha256' \) \
  -mtime "+${RETENTION_DAYS}" -delete

echo "BACKUP_FILE=${encrypted_file}"
echo "SHA256_FILE=${checksum_file}"
