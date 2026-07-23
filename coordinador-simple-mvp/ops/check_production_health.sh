#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="${PROJECT_DIR:-/opt/coordinador-simple-mvp}"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.prod.yml}"
ENV_FILE="${ENV_FILE:-.env.prod}"
PUBLIC_URL="${PUBLIC_URL:-https://coordina.xshift007.com}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/tavi-coordina}"
BACKUP_MAX_AGE_MINUTES="${BACKUP_MAX_AGE_MINUTES:-1800}"
DISK_MAX_PERCENT="${DISK_MAX_PERCENT:-85}"
STATE_DIR="${STATE_DIRECTORY:-/var/lib/tavi-coordina-monitor}"

declare -a failures=()
declare -a degraded=()
declare -A service_health=()
public_degraded=0
gateway_degraded=0
services_degraded=0

require_command() {
  command -v "$1" >/dev/null 2>&1 || failures+=("falta comando: $1")
}

json_array() {
  if (($# == 0)); then
    printf '[]'
  else
    printf '%s\n' "$@" | jq -R . | jq -s .
  fi
}

for command_name in docker curl jq sha256sum find; do
  require_command "$command_name"
done

mkdir -p "$STATE_DIR"

if ((${#failures[@]} == 0)); then
  cd "$PROJECT_DIR"
  compose=(docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE")

  if ! "${compose[@]}" config --quiet; then
    failures+=("docker compose config invalido")
  fi

  expected_services=$'backend\ncaddy\ndb\nfrontend\ngateway'
  actual_services="$("${compose[@]}" config --services 2>/dev/null | sort || true)"
  if [[ "$actual_services" != "$expected_services" ]]; then
    failures+=("conjunto de servicios inesperado")
  fi

  for service in db backend frontend gateway caddy; do
    container_id="$("${compose[@]}" ps -q "$service" 2>/dev/null || true)"
    if [[ -z "$container_id" ]]; then
      failures+=("$service sin contenedor")
      continue
    fi

    state="$(docker inspect --format '{{.State.Status}}' "$container_id" 2>/dev/null || true)"
    health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$container_id" 2>/dev/null || true)"
    service_health["$service"]="$health"
    if [[ "$state" != "running" ]]; then
      failures+=("$service estado=$state")
    elif [[ "$health" == "none" ]]; then
      failures+=("$service sin healthcheck")
    elif [[ "$health" == "starting" ]]; then
      degraded+=("$service health=starting")
      services_degraded=1
    elif [[ "$health" != "healthy" ]]; then
      if [[ "$service" == "gateway" ]]; then
        degraded+=("gateway health=$health")
        gateway_degraded=1
      else
        failures+=("$service health=$health")
      fi
    fi
  done

  db_id="$("${compose[@]}" ps -q db 2>/dev/null || true)"
  if [[ -n "$db_id" && "${service_health[db]:-none}" != "starting" ]] && ! docker exec "$db_id" sh -lc \
    'psql --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" --tuples-only --no-align --command="SELECT 1"' \
    | grep -qx '1'; then
    failures+=("postgres SELECT 1 fallo")
  fi

  backend_id="$("${compose[@]}" ps -q backend 2>/dev/null || true)"
  if [[ -n "$backend_id" && "${service_health[backend]:-none}" != "starting" ]] && ! docker exec "$backend_id" python -c \
    "import json,urllib.request; data=json.load(urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=3)); assert data.get('ok') is True"; then
    failures+=("backend readiness fallo")
  fi

  frontend_id="$("${compose[@]}" ps -q frontend 2>/dev/null || true)"
  if [[ -n "$frontend_id" && "${service_health[frontend]:-none}" != "starting" ]] && ! docker exec "$frontend_id" sh -lc \
    "wget -q -O - http://127.0.0.1/ | grep -q '<div id=\"root\"></div>'"; then
    failures+=("frontend interno fallo")
  fi

  caddy_id="$("${compose[@]}" ps -q caddy 2>/dev/null || true)"
  if [[ -n "$caddy_id" && "${service_health[caddy]:-none}" != "starting" ]] && ! docker exec "$caddy_id" caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null 2>&1; then
    failures+=("caddy config invalida")
  fi

  public_code="$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' --max-time 10 "$PUBLIC_URL/" || true)"
  if [[ "$public_code" != "200" ]]; then
    degraded+=("HTTPS publico codigo=$public_code")
    public_degraded=1
  fi

  protected_code="$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' --max-time 10 "$PUBLIC_URL/api/runtime" || true)"
  if [[ "$protected_code" == "000" ]]; then
    degraded+=("API protegida no alcanzable codigo=000")
    public_degraded=1
  elif [[ "$protected_code" != "401" ]]; then
    failures+=("API sin auth codigo=$protected_code")
  fi

  gateway_id="$("${compose[@]}" ps -q gateway 2>/dev/null || true)"
  if [[ -n "$gateway_id" ]]; then
    gateway_json="$(docker exec "$gateway_id" node -e \
      "fetch('http://127.0.0.1:8080/status').then(r=>r.text()).then(console.log).catch(()=>process.exit(1))" 2>/dev/null || true)"
    gateway_state="$(jq -r '.state // "unavailable"' <<<"$gateway_json" 2>/dev/null || printf 'unavailable')"
    gateway_pending="$(jq -r '.pendingMessages // 0' <<<"$gateway_json" 2>/dev/null || printf '0')"
    gateway_dead="$(jq -r '.deadLetterMessages // 0' <<<"$gateway_json" 2>/dev/null || printf '0')"
    gateway_overflow="$(jq -r '.queueOverflowCount // 0' <<<"$gateway_json" 2>/dev/null || printf '0')"
    if [[ "$gateway_state" == "logged_out" ]]; then
      failures+=("gateway desvinculado de WhatsApp")
    elif [[ "$gateway_state" != "connected" ]]; then
      degraded+=("gateway estado=$gateway_state")
      gateway_degraded=1
    fi
    if ((gateway_dead > 0 || gateway_overflow > 0)); then
      failures+=("gateway cola con dead-letter=${gateway_dead} overflow=${gateway_overflow}")
    elif ((gateway_pending >= 100)); then
      degraded+=("gateway cola pendiente=${gateway_pending}")
      gateway_degraded=1
    fi
  fi

  newest_checksum="$(find "$BACKUP_DIR" -maxdepth 1 -type f -name 'tavi-coordina-*.cms.sha256' -mmin "-${BACKUP_MAX_AGE_MINUTES}" -printf '%T@ %p\n' 2>/dev/null | sort -nr | head -n 1 | cut -d' ' -f2- || true)"
  newest_backup="${newest_checksum%.sha256}"
  if [[ -z "$newest_checksum" || ! -f "$newest_backup" ]]; then
    failures+=("respaldo cifrado ausente o antiguo")
  elif ! (cd "$BACKUP_DIR" && sha256sum --check --status "$(basename "$newest_checksum")"); then
    failures+=("checksum del ultimo respaldo fallo")
  fi

  disk_percent="$(df --output=pcent "$PROJECT_DIR" | tail -n 1 | tr -dc '0-9')"
  if [[ -n "$disk_percent" ]] && ((disk_percent >= DISK_MAX_PERCENT)); then
    failures+=("disco al ${disk_percent}%")
  fi
fi

update_counter() {
  local name="$1"
  local active="$2"
  local counter_file="$STATE_DIR/degraded-${name}-count"
  local previous
  previous="$(cat "$counter_file" 2>/dev/null || printf '0')"
  [[ "$previous" =~ ^[0-9]+$ ]] || previous=0
  local current=0
  if ((active)); then
    current=$((previous + 1))
  fi
  printf '%s\n' "$current" >"$counter_file"
  printf '%s' "$current"
}

public_count="$(update_counter public "$public_degraded")"
gateway_count="$(update_counter gateway "$gateway_degraded")"
services_count="$(update_counter services "$services_degraded")"

((public_count >= 3)) && failures+=("HTTPS degradado por tres ciclos")
((gateway_count >= 3)) && failures+=("gateway degradado por tres ciclos")
((services_count >= 3)) && failures+=("servicios iniciando por tres ciclos")

status="ok"
if ((${#failures[@]} > 0)); then
  status="failed"
elif ((${#degraded[@]} > 0)); then
  status="degraded"
fi

failures_json="$(json_array "${failures[@]}")"
degraded_json="$(json_array "${degraded[@]}")"
result_tmp="$(mktemp "$STATE_DIR/.last-result.XXXXXX")"
jq -n \
  --arg checked_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg status "$status" \
  --argjson public_count "$public_count" \
  --argjson gateway_count "$gateway_count" \
  --argjson services_count "$services_count" \
  --argjson failures "$failures_json" \
  --argjson degraded "$degraded_json" \
  '{checked_at:$checked_at,status:$status,degraded_counts:{public:$public_count,gateway:$gateway_count,services:$services_count},failures:$failures,degraded:$degraded}' \
  >"$result_tmp"
mv -f "$result_tmp" "$STATE_DIR/last-result.json"

if [[ "$status" == "failed" ]]; then
  echo "TAVI Coordina monitor FAILED: ${failures[*]}" >&2
  exit 1
fi

echo "TAVI Coordina monitor ${status^^}: ${degraded[*]:-todos los checks pasaron}"
