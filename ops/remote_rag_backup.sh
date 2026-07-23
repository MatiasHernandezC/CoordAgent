#!/usr/bin/env bash
set -euo pipefail
cd /opt/coordinador-simple-mvp
TS="$(date -u +%Y%m%dT%H%M%SZ)"
BK="/opt/coordinador-simple-mvp/.deploy-backup/rag-${TS}"
mkdir -p "${BK}/backend/app/services" "${BK}/backend/app/bff" "${BK}/frontend/src"
cp -a backend/app/schemas.py "${BK}/backend/app/"
cp -a backend/app/bff/routes.py "${BK}/backend/app/bff/"
cp -a backend/app/services/llm_service.py backend/app/services/session_service.py "${BK}/backend/app/services/"
cp -a frontend/src/App.tsx frontend/src/types.ts frontend/src/styles.css "${BK}/frontend/src/" || true
if [[ -f backend/app/services/group_memory.py ]]; then
  cp -a backend/app/services/group_memory.py "${BK}/backend/app/services/" || true
fi
docker image tag coordinador-simple-mvp-backend:latest "coordinador-simple-mvp-backend:pre-rag-${TS}"
docker image tag coordinador-simple-mvp-frontend:latest "coordinador-simple-mvp-frontend:pre-rag-${TS}"
echo "BACKUP=${BK}"
echo "TS=${TS}"
ls -la "${BK}/backend/app/services"
docker images | grep pre-rag | head -10
