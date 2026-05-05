from datetime import datetime, timedelta

from app.schemas import AvailabilityCell, Participant, Session, TimeOption, TimeSlot

WEEKDAYS = ["lunes", "martes", "miercoles", "jueves", "viernes"]
WORKDAY_START = 9
WORKDAY_END = 18


def calculate_options(session: Session) -> list[TimeOption]:
    participant_names = [participant.name for participant in session.participants]
    matrix = build_availability_matrix(session)

    options: list[TimeOption] = []
    for cell in matrix:
        if cell.score == 0:
            continue

        options.append(
            TimeOption(
                day=cell.day,
                start=cell.start,
                end=cell.end,
                available_participants=cell.available_participants,
                unavailable_participants=cell.unavailable_participants,
                score=cell.score,
                coverage_percent=cell.coverage_percent,
                explanation=build_explanation(
                    cell.available_participants,
                    cell.unavailable_participants,
                    cell.coverage_percent,
                ),
            )
        )

    return sorted(options, key=lambda option: (-option.score, option.day, option.start))[:3]


def build_availability_matrix(session: Session) -> list[AvailabilityCell]:
    participant_names = [participant.name for participant in session.participants]
    scores: dict[tuple[str, str, str], set[str]] = {}

    for participant in session.participants:
        for slot in participant.availability:
            for block in split_into_hour_blocks(slot):
                key = (block.day, block.start, block.end)
                scores.setdefault(key, set()).add(participant.name)

    matrix: list[AvailabilityCell] = []
    for day in WEEKDAYS:
        for hour in range(WORKDAY_START, WORKDAY_END):
            start = f"{hour:02d}:00"
            end = f"{hour + 1:02d}:00"
            names = scores.get((day, start, end), set())
            available = sorted(names)
            unavailable = sorted(name for name in participant_names if name not in names)
            coverage_percent = round((len(available) / max(len(participant_names), 1)) * 100)
            matrix.append(
                AvailabilityCell(
                    day=day,  # type: ignore[arg-type]
                    start=start,
                    end=end,
                    available_participants=available,
                    unavailable_participants=unavailable,
                    score=len(available),
                    coverage_percent=coverage_percent,
                )
            )

    return matrix


def find_missing_info(session: Session) -> list[str]:
    missing: list[str] = []
    if not session.participants:
        missing.append("Agrega participantes o escribe un mensaje con disponibilidades.")

    for participant in session.participants:
        if not participant.availability:
            missing.append(f"Falta disponibilidad de {participant.name}.")

    return missing


def build_insights(session: Session) -> list[str]:
    insights: list[str] = []

    if not session.participants:
        return ["Aun no hay participantes detectados."]

    insights.append(f"Se detectaron {len(session.participants)} participante(s).")

    if session.missing_info:
        insights.append(f"Hay {len(session.missing_info)} dato(s) faltante(s) antes de una decision ideal.")

    if session.options:
        best = session.options[0]
        insights.append(
            f"La mejor opcion actual cubre {best.coverage_percent}% del grupo: {best.day} {best.start}-{best.end}."
        )
        if best.unavailable_participants:
            insights.append(f"Quedan fuera en la mejor opcion: {', '.join(best.unavailable_participants)}.")
        else:
            insights.append("La mejor opcion incluye a todos los participantes.")

    return insights


def build_explanation(available: list[str], unavailable: list[str], coverage_percent: int) -> str:
    if not unavailable:
        return f"Todos los participantes pueden asistir. Cobertura {coverage_percent}%."

    return (
        f"Asisten {len(available)} participante(s): {', '.join(available)}. "
        f"No calzan: {', '.join(unavailable)}. Cobertura {coverage_percent}%."
    )


def split_into_hour_blocks(slot: TimeSlot) -> list[TimeSlot]:
    start = parse_time(slot.start)
    end = parse_time(slot.end)
    blocks: list[TimeSlot] = []

    current = start
    while current + timedelta(hours=1) <= end:
        next_time = current + timedelta(hours=1)
        blocks.append(
            TimeSlot(
                day=slot.day,
                start=current.strftime("%H:%M"),
                end=next_time.strftime("%H:%M"),
            )
        )
        current = next_time

    return blocks


def parse_time(value: str) -> datetime:
    return datetime.strptime(value, "%H:%M")
