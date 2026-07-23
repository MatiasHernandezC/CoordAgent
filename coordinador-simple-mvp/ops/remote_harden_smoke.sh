#!/usr/bin/env bash
set -euo pipefail
cd /opt/coordinador-simple-mvp

docker compose -f docker-compose.prod.yml --env-file .env.prod exec -T backend python - <<'PY'
from app.schemas import (
    DecisionRecord,
    ExtractedAvailability,
    Participant,
    Session,
    TimeOption,
    TimeSlot,
)
from app.services.group_memory import apply_memory_identity, build_group_memory_context
from app.services.llm_service import LlmService, extract_json
from app.services.temporal_grounding import validate_extracted_temporal_grounding
import json

service = LlmService()

# A: time only — no invented day
print("=== CASE A time without day ===")
fake = ExtractedAvailability(
    participants=[Participant(name="Yo", availability=[TimeSlot(day="lunes", start="18:00", end="20:00")])]
)
grounded = validate_extracted_temporal_grounding(fake, "Puedo despues de las 18.")
print("source", grounded.grounding_source, "rejected", grounded.rejected_days)
assert grounded.extraction.participants[0].availability == []
ext, src, _ = service.extract_availability("Puedo despues de las 18.")
days = {s.day for p in ext.participants for s in p.availability}
print("live_source", src, "days", days)
assert days == set()

# B: active round day
print("=== CASE B active round ===")
fake_b = ExtractedAvailability(
    participants=[Participant(name="Yo", availability=[TimeSlot(day="jueves", start="18:00", end="20:00")])]
)
g_b = validate_extracted_temporal_grounding(
    fake_b,
    "Yo despues de las 18.",
    active_round_context="Estamos coordinando para el jueves.",
)
print("source", g_b.grounding_source, "slots", g_b.extraction.participants[0].availability)
assert g_b.extraction.participants[0].availability[0].day == "jueves"

# C: historical decision must not ground martes
print("=== CASE C history conflict ===")
mem = build_group_memory_context(
    Session(
        title="hist",
        participants=[Participant(name="Nicolas")],
        decision_history=[
            DecisionRecord(
                option=TimeOption(
                    day="martes", start="18:00", end="19:00",
                    available_participants=["Nicolas"], score=1, coverage_percent=100,
                ),
                summary="prev",
                source="panel",
            )
        ],
    )
)
assert mem.past_decisions
ext_c, src_c, _ = service.extract_availability(
    "Esta semana solo puedo el viernes a las 17.",
    group_memory=mem,
    active_round_context="La reunion anterior fue el martes.",
)
days_c = {s.day for p in ext_c.participants for s in p.availability}
print("source", src_c, "days", days_c, "flags", ext_c.quality_flags)
assert "martes" not in days_c

# D: alias known
print("=== CASE D alias ===")
mem_d = build_group_memory_context(
    Session(title="alias", participants=[Participant(name="Julissa (Yuli)"), Participant(name="Nicolas")])
)
ext_d, src_d, _ = service.extract_availability(
    "Yuli puede el jueves de 15 a 18.",
    group_memory=mem_d,
)
print("source", src_d, "people", [p.name for p in ext_d.participants])
# post-process alias even if mock/gemini returns Yuli
resolved = apply_memory_identity(
    ExtractedAvailability(participants=[Participant(name="Yuli", availability=[TimeSlot(day="jueves", start="15:00", end="18:00")])]),
    mem_d,
)
assert resolved.participants[0].name == "Julissa (Yuli)"

# E: unknown alias
print("=== CASE E unknown alias ===")
resolved_e = apply_memory_identity(
    ExtractedAvailability(participants=[Participant(name="Cata")]),
    mem_d,
)
assert resolved_e.participants[0].name == "Cata"

# F: fenced JSON parse (local unit, no extra network)
print("=== CASE F fenced json ===")
payload = extract_json('```json\n{"entries":[]}\n```')
assert json.loads(payload) == {"entries": []}

print("HARDEN_SMOKE_OK")
PY

echo "=== health ==="
docker inspect --format='backend={{.State.Health.Status}}' coordinador-simple-mvp-backend-1
docker inspect --format='gateway={{.State.Health.Status}}' coordinador-simple-mvp-gateway-1
docker inspect --format='frontend={{.State.Health.Status}}' coordinador-simple-mvp-frontend-1
docker stats --no-stream --format 'table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}' | head -8
docker compose -f docker-compose.prod.yml --env-file .env.prod logs --since 10m backend 2>&1 | grep -E 'ERROR|Traceback|temporal_grounding|gemini_json' | tail -20 || true
