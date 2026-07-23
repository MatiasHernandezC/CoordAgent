#!/usr/bin/env bash
set -euo pipefail
cd /opt/coordinador-simple-mvp
TS="$(date -u +%Y%m%dT%H%M%SZ)"
BK=".deploy-backup/harden-${TS}"
mkdir -p "${BK}/services"
cp -a backend/app/services/llm_service.py backend/app/services/group_memory.py "${BK}/services/" 2>/dev/null || true
cp -a backend/app/services/temporal_grounding.py "${BK}/services/" 2>/dev/null || true
docker image tag coordinador-simple-mvp-backend:latest "coordinador-simple-mvp-backend:pre-harden-${TS}" || true
echo "BACKUP=${BK}"
test -f backend/app/services/temporal_grounding.py
docker compose -f docker-compose.prod.yml --env-file .env.prod build backend
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d --no-deps backend
for i in 1 2 3 4 5 6 7 8 9 10; do
  sleep 3
  st=$(docker inspect --format='{{.State.Health.Status}}' coordinador-simple-mvp-backend-1 2>/dev/null || echo missing)
  echo "backend=$st"
  [[ "$st" == "healthy" ]] && break
done
docker compose -f docker-compose.prod.yml --env-file .env.prod ps
docker inspect --format='gateway={{.State.Health.Status}}' coordinador-simple-mvp-gateway-1
