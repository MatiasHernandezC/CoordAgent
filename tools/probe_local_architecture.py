"""Sonda end-to-end de arquitectura usando un modelo local real.

No compara modelos; asume `qwen` como candidato local y prueba que el flujo de
Coordina completo se mantenga sano en casos dificiles:

mensaje -> LLM local -> parser/normalizador -> merge incremental -> calculo.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.services.llm_service import LlmService  # noqa: E402
from app.services.session_service import SessionService  # noqa: E402
from app.settings import settings  # noqa: E402
from app.storage.json_repository import JsonRepository  # noqa: E402
import app.services.session_service as session_module  # noqa: E402


SCENARIOS = [
    {
        "id": "incremental_hard_group",
        "messages": [
            "Ana puede cualquier dia menos el viernes",
            "Luis no puede despues de las 4 el lunes",
            "Camila sale de turno a las 14 el lunes",
            "Sofia toda la semana esta full, solo le queda el viernes despues de las 3",
            "Pedro vuelve de la clinica tipo 11 el miercoles, de ahi en adelante libre",
        ],
        "expected_people": {
            "Ana": [
                ("lunes", "09:00", "18:00"),
                ("martes", "09:00", "18:00"),
                ("miercoles", "09:00", "18:00"),
                ("jueves", "09:00", "18:00"),
            ],
            "Luis": [("lunes", "09:00", "16:00")],
            "Camila": [("lunes", "14:00", "18:00")],
            "Sofia": [("viernes", "15:00", "18:00")],
            "Pedro": [("miercoles", "11:00", "18:00")],
        },
        "expected_best": ("lunes", "14:00", "15:00"),
    },
    {
        "id": "correction_after_confirmation",
        "messages": [
            "Ana puede lunes de 10 a 12",
            "Luis puede lunes de 10 a 12",
        ],
        "post_confirm_messages": [
            "Luis solo puede martes de 10 a 12",
        ],
        "expected_stale_cleared": True,
    },
]


def configure_local(model: str, timeout: int) -> None:
    settings.llm_provider = "local"
    settings.local_llm_model = model
    settings.local_llm_timeout_seconds = timeout
    settings.llm_cache_enabled = False
    settings.llm_fallback_enabled = False
    settings.db_backend = "json"


def availability_map(session) -> dict[str, list[tuple[str, str, str]]]:
    return {
        participant.name: sorted((slot.day, slot.start, slot.end) for slot in participant.availability)
        for participant in session.participants
    }


def apply_message(service: SessionService, llm: LlmService, session, message: str):
    started = time.perf_counter()
    extraction, source, _ = llm.extract_availability(message)
    session = service.merge_extraction(session.id, extraction, message, source)
    elapsed = time.perf_counter() - started
    return session, {"message": message, "source": source, "seconds": round(elapsed, 2)}


def run_scenario(scenario: dict, model: str, timeout: int) -> dict:
    configure_local(model, timeout)
    with tempfile.TemporaryDirectory(prefix="coordina-arch-probe-") as tmp:
        session_module.repository = JsonRepository(Path(tmp) / "sessions.json")
        service = SessionService()
        llm = LlmService()
        session = service.create(scenario["id"])
        steps = []

        for message in scenario["messages"]:
            session, step = apply_message(service, llm, session, message)
            steps.append(step)

        session = service.calculate(session.id)
        result = {
            "id": scenario["id"],
            "steps": steps,
            "people": availability_map(session),
            "options": [
                {
                    "day": option.day,
                    "start": option.start,
                    "end": option.end,
                    "coverage_percent": option.coverage_percent,
                    "available": option.available_participants,
                    "unavailable": option.unavailable_participants,
                }
                for option in session.options
            ],
            "passed": True,
            "errors": [],
        }

        expected_people = scenario.get("expected_people")
        if expected_people:
            for name, expected_slots in expected_people.items():
                actual = result["people"].get(name)
                if actual != sorted(expected_slots):
                    result["passed"] = False
                    result["errors"].append({"person": name, "expected": expected_slots, "actual": actual})

        expected_best = scenario.get("expected_best")
        if expected_best:
            best = session.options[0] if session.options else None
            actual_best = (best.day, best.start, best.end) if best else None
            if actual_best != expected_best:
                result["passed"] = False
                result["errors"].append({"best_expected": expected_best, "best_actual": actual_best})

        if scenario.get("post_confirm_messages"):
            if not session.options:
                result["passed"] = False
                result["errors"].append({"confirm": "no options before confirmation"})
            else:
                session = service.confirm(session.id, session.options[0].id)
                for message in scenario["post_confirm_messages"]:
                    session, step = apply_message(service, llm, session, message)
                    steps.append(step)
                stale_cleared = session.selected_option is None and session.decision_summary is None
                if scenario.get("expected_stale_cleared") and not stale_cleared:
                    result["passed"] = False
                    result["errors"].append({"stale_decision": "not cleared"})
                result["post_confirm_status"] = session.status
                result["stale_cleared"] = stale_cleared
                result["people_after_correction"] = availability_map(session)

        return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="qwen")
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument("--scenario", choices=[item["id"] for item in SCENARIOS], default=None)
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "data" / "local_architecture_probe.json")
    args = parser.parse_args()

    scenarios = SCENARIOS
    if args.scenario:
        scenarios = [item for item in SCENARIOS if item["id"] == args.scenario]

    results = [run_scenario(scenario, args.model, args.timeout) for scenario in scenarios]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    for result in results:
        status = "PASS" if result["passed"] else "FAIL"
        seconds = sum(step["seconds"] for step in result["steps"])
        print(f"{status} {result['id']} total_seconds={seconds:.2f}")
        if result["errors"]:
            print(json.dumps(result["errors"], ensure_ascii=False, indent=2))
    print(f"Reporte escrito en {args.out}")


if __name__ == "__main__":
    main()
