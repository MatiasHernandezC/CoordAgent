#!/usr/bin/env bash
set -euo pipefail
cd /opt/coordinador-simple-mvp

# Sanity: RAG module present
test -f backend/app/services/group_memory.py
grep -q 'retrieval_used' backend/app/schemas.py
grep -q 'build_group_memory_context' backend/app/bff/routes.py

echo "Building backend + frontend only..."
docker compose -f docker-compose.prod.yml --env-file .env.prod build backend frontend

echo "Recreating backend + frontend..."
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d --no-deps backend frontend

echo "Waiting for health..."
for i in 1 2 3 4 5 6 7 8 9 10 11 12; do
  sleep 5
  backend_status=$(docker inspect --format='{{.State.Health.Status}}' coordinador-simple-mvp-backend-1 2>/dev/null || echo missing)
  frontend_status=$(docker inspect --format='{{.State.Health.Status}}' coordinador-simple-mvp-frontend-1 2>/dev/null || echo missing)
  echo "try=$i backend=$backend_status frontend=$frontend_status"
  if [[ "$backend_status" == "healthy" && "$frontend_status" == "healthy" ]]; then
    break
  fi
done

docker compose -f docker-compose.prod.yml --env-file .env.prod ps
echo "Recent backend logs:"
docker compose -f docker-compose.prod.yml --env-file .env.prod logs --tail 40 backend
