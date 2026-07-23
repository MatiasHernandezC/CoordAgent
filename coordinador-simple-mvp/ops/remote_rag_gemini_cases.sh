#!/usr/bin/env bash
set -euo pipefail
cd /opt/coordinador-simple-mvp

docker compose -f docker-compose.prod.yml --env-file .env.prod exec -T backend python - <<'PY'
import json
from app.schemas import (
    ChannelConfig,
    ChannelMessage,
    DecisionRecord,
    Participant,
    Session,
    TimeOption,
    TimeSlot,
)
from app.services.group_memory import build_group_memory_context, format_group_memory_preview
from app.services.llm_service import LlmService
from app.services.decision_engine import calculate_options

service = LlmService()

# --- Alias case ---
session_alias = Session(
    title="Gemini alias case",
    channel_config=ChannelConfig(group_name="Demo alias", group_participant_count=3),
    participants=[
        Participant(name="Nicolas"),
        Participant(name="Julissa (Yuli)"),
        Participant(name="Matias"),
    ],
    channel_messages=[
        ChannelMessage(sender="Nicolas", text="listos?", kind="human"),
        ChannelMessage(sender="Yuli", text="aca", kind="human"),
    ],
)
mem = build_group_memory_context(session_alias)
print("CASE_ALIAS_PREVIEW", format_group_memory_preview(mem))
ext, source, usage = service.extract_availability(
    "Yuli puede despues de las 15 y Nicolas desde las 16.",
    group_memory=mem,
)
print("CASE_ALIAS_SOURCE", source)
print("CASE_ALIAS_PEOPLE", [p.name for p in ext.participants])
print(
    "CASE_ALIAS_SLOTS",
    {p.name: [(s.day, s.start, s.end) for s in p.availability] for p in ext.participants},
)
if usage:
    print("CASE_ALIAS_TOKENS", usage.total_tokens, "cached", usage.cached)

# --- Historical decision case ---
session_hist = Session(
    title="Gemini history case",
    participants=[Participant(name="Nicolas"), Participant(name="Julissa")],
    decision_history=[
        DecisionRecord(
            option=TimeOption(
                day="martes",
                start="18:00",
                end="19:00",
                available_participants=["Nicolas", "Julissa"],
                score=2,
                coverage_percent=100,
            ),
            summary="prev meeting",
            source="panel",
        )
    ],
)
mem2 = build_group_memory_context(session_hist)
print("CASE_HIST_PREVIEW", format_group_memory_preview(mem2))
print("CASE_HIST_PAST", mem2.past_decisions)
ext2, source2, usage2 = service.extract_availability(
    "Esta semana solo puedo el jueves despues de las 15.",
    group_memory=mem2,
)
print("CASE_HIST_SOURCE", source2)
print("CASE_HIST_PEOPLE", [p.name for p in ext2.participants])
days = sorted({s.day for p in ext2.participants for s in p.availability})
print("CASE_HIST_DAYS", days)
# Critical anti-hallucination: do not invent martes from historical decision alone.
assert "martes" not in days, f"historical martes leaked into extraction: {days}"

# --- Vague identity ---
ext3, source3, _ = service.extract_availability(
    "El puede en la tarde.",
    group_memory=build_group_memory_context(Session(title="vague", participants=[Participant(name="Nicolas")])),
)
print("CASE_VAGUE_SOURCE", source3)
print("CASE_VAGUE_PEOPLE", [p.name for p in ext3.participants])

# Engine still deterministic
opts = calculate_options(
    Session(
        title="engine",
        participants=[
            Participant(name="Julissa", availability=[TimeSlot(day="jueves", start="15:00", end="18:00")]),
            Participant(name="Nicolas", availability=[TimeSlot(day="jueves", start="16:00", end="18:00")]),
        ],
    )
)
print("CASE_ENGINE", [(o.day, o.start, o.end, o.coverage_percent) for o in opts[:2]])
assert opts and opts[0].coverage_percent == 100
print("GEMINI_CASES_OK")
PY

echo "=== backend errors last 15m ==="
docker compose -f docker-compose.prod.yml --env-file .env.prod logs --since 15m backend 2>&1 | grep -E 'ERROR|Traceback|Exception' | tail -20 || true
echo "=== health monitor now ==="
# force a quick health view
docker compose -f docker-compose.prod.yml --env-file .env.prod ps
docker stats --no-stream --format 'table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}' | head -10
