#!/usr/bin/env bash
set -euo pipefail
cd /opt/coordinador-simple-mvp
docker compose -f docker-compose.prod.yml --env-file .env.prod build backend
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d --no-deps backend
for i in 1 2 3 4 5 6 7 8; do
  sleep 3
  status=$(docker inspect --format='{{.State.Health.Status}}' coordinador-simple-mvp-backend-1 2>/dev/null || echo missing)
  echo "backend_health=$status"
  [[ "$status" == "healthy" ]] && break
done
docker compose -f docker-compose.prod.yml --env-file .env.prod exec -T backend python - <<'PY'
from app.schemas import ExtractedAvailability, Participant, Session, TimeSlot
from app.services.group_memory import apply_memory_identity, build_group_memory_context
from app.services.llm_service import LlmService

memory = build_group_memory_context(
    Session(title="t", participants=[Participant(name="Julissa (Yuli)"), Participant(name="Nicolas")])
)
resolved = apply_memory_identity(
    ExtractedAvailability(
        participants=[
            Participant(name="Yuli", availability=[TimeSlot(day="jueves", start="15:00", end="18:00")])
        ]
    ),
    memory,
)
print("resolved_name=", resolved.participants[0].name)
assert resolved.participants[0].name == "Julissa (Yuli)"

service = LlmService()
extraction, source, _ = service.extract_availability(
    "Yuli puede el jueves de 15 a 18.",
    group_memory=memory,
)
print("source=", source)
print("people=", [p.name for p in extraction.participants])
print("ALIAS_MAP_OK")
PY
docker inspect --format='{{.State.Health.Status}}' coordinador-simple-mvp-gateway-1
