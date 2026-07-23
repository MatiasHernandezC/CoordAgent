#!/usr/bin/env bash
set -euo pipefail
cd /opt/coordinador-simple-mvp

echo "=== compose ps ==="
docker compose -f docker-compose.prod.yml --env-file .env.prod ps

echo "=== ready endpoint inside backend ==="
docker compose -f docker-compose.prod.yml --env-file .env.prod exec -T backend \
  python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=5).read().decode())"

echo "=== group_memory import ==="
docker compose -f docker-compose.prod.yml --env-file .env.prod exec -T backend \
  python - <<'PY'
from app.schemas import ChannelConfig, DecisionRecord, Participant, Session, TimeOption, TimeSlot, ChannelMessage
from app.services.group_memory import build_group_memory_context, format_group_memory_for_prompt, format_group_memory_preview, memory_fingerprint
from app.services.llm_service import LlmService, build_extraction_prompt, build_cache_key
from app.services.decision_engine import calculate_options

session = Session(
    title="Smoke RAG",
    channel_config=ChannelConfig(group_name="Smoke Group", group_participant_count=3),
    participants=[
        Participant(name="Nicolas"),
        Participant(name="Julissa (Yuli)"),
        Participant(name="Matias"),
    ],
    decision_history=[
        DecisionRecord(
            option=TimeOption(
                day="martes", start="18:00", end="19:00",
                available_participants=["Nicolas", "Julissa"],
                score=2, coverage_percent=66,
            ),
            summary="prev",
            source="panel",
        )
    ],
    channel_messages=[ChannelMessage(sender="Yuli", text="hola", kind="human")],
)
memory = build_group_memory_context(session)
prompt = build_extraction_prompt(
    "Yuli puede despues de las 16 y yo desde las 17.",
    group_memory=memory,
)
assert memory.has_content()
assert "Yuli" in prompt
assert "martes 18:00-19:00" in prompt or "martes" in prompt
assert "NO inventes disponibilidad" in prompt
assert "NO conviertas decisiones historicas" in prompt
fp1 = memory_fingerprint(memory)
memory.retrieved_at = "2099-01-01T00:00:00+00:00"
fp2 = memory_fingerprint(memory)
assert fp1 == fp2
key_a = build_cache_key("msg", 9, 18, memory_fingerprint=fp1)
key_b = build_cache_key("msg", 9, 18, memory_fingerprint="other")
assert key_a != key_b
print("memory_preview=", format_group_memory_preview(memory))
print("fingerprint_ok")

service = LlmService()
extraction, source, usage = service.extract_availability(
    "Esta semana solo puedo el jueves despues de las 15.",
    group_memory=memory,
)
print("extract_source=", source)
print("people=", [p.name for p in extraction.participants])
days = sorted({s.day for p in extraction.participants for s in p.availability})
print("days=", days)
# Historical martes should not appear as extracted availability from mock/gemini
# for this text (only jueves). Soft check for mock.
if "mock" in source:
    assert "jueves" in days or not days

engine_session = Session(
    title="engine",
    participants=[
        Participant(name="A", availability=[TimeSlot(day="jueves", start="15:00", end="18:00")]),
        Participant(name="B", availability=[TimeSlot(day="jueves", start="16:00", end="18:00")]),
    ],
)
opts = calculate_options(engine_session)
print("engine_options=", [(o.day, o.start, o.end, o.coverage_percent) for o in opts[:2]])
assert opts
print("SMOKE_MODULE_OK")
PY

echo "=== API flow with auth from env (no secrets printed) ==="
docker compose -f docker-compose.prod.yml --env-file .env.prod exec -T backend \
  python - <<'PY'
import os
import json
import urllib.request
from base64 import b64encode

# Call loopback API without basic auth (internal network). Caddy protects public.
base = "http://127.0.0.1:8000"

def req(method, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        base + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if body is not None else {},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode())

# create session
created = req("POST", "/api/sessions", {"title": "RAG droplet smoke"})
sid = created["session"]["id"]
req("POST", f"/api/sessions/{sid}/participants", {"name": "Nicolas"})
req("POST", f"/api/sessions/{sid}/participants", {"name": "Julissa (Yuli)"})
req("POST", f"/api/sessions/{sid}/participants", {"name": "Matias"})
# seed decision via calculate path if available is hard; use message then force second
first = req("POST", f"/api/sessions/{sid}/message", {"message": "Nicolas puede jueves de 15 a 18 y Julissa puede jueves de 15 a 18"})
proc1 = first["session"]["last_processing"]
second = req("POST", f"/api/sessions/{sid}/message", {"message": "Yuli puede el viernes despues de las 16"})
proc2 = second["session"]["last_processing"]
print("llm_source_1=", first.get("llm_source"))
print("llm_source_2=", second.get("llm_source"))
print("retrieval_used=", proc2.get("retrieval_used"))
print("retrieval_source=", proc2.get("retrieval_source"))
print("retrieval_participant_count=", proc2.get("retrieval_participant_count"))
print("retrieval_past_decision_count=", proc2.get("retrieval_past_decision_count"))
print("retrieval_preview=", proc2.get("retrieval_preview"))
print("session_id=", sid)
assert proc2.get("retrieval_used") is True
assert proc2.get("retrieval_participant_count", 0) >= 2
assert "roster=" in (proc2.get("retrieval_preview") or "")
# cleanup: archive if endpoint exists
try:
    req("POST", f"/api/sessions/{sid}/archive")
    print("archived_smoke_session")
except Exception as error:
    print("archive_skip=", type(error).__name__)
print("SMOKE_API_OK")
PY

echo "=== gateway still healthy? ==="
docker inspect --format='{{.State.Health.Status}}' coordinador-simple-mvp-gateway-1
docker compose -f docker-compose.prod.yml --env-file .env.prod logs --since 10m --tail 20 gateway | tail -20

echo "=== monitor ==="
cat /var/lib/tavi-coordina-monitor/last-result.json 2>/dev/null || true
