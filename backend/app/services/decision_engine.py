from datetime import datetime, timedelta

from app.schemas import AvailabilityCell, Participant, Session, TimeOption, TimeSlot

WEEKDAYS = ["lunes", "martes", "miercoles", "jueves", "viernes"]
WORKDAY_START = 9
WORKDAY_END = 18


def calculate_options(session: Session) -> list[TimeOption]:
    return options_from_matrix(build_availability_matrix(session))


def options_from_matrix(matrix: list[AvailabilityCell]) -> list[TimeOption]:
    # Diversifica: un solo bloque (el mejor) por (semana, dia). Asi la misma
    # persona en dias/semanas distintos no genera opciones redundantes, y dos
    # semanas no colisionan. Ante empate de score se conserva el mas temprano.
    best_by_slot: dict[tuple[int, str], AvailabilityCell] = {}
    for cell in matrix:
        if cell.score == 0:
            continue
        key = (cell.week_offset, cell.day)
        best = best_by_slot.get(key)
        if best is None or cell.score > best.score:
            best_by_slot[key] = cell

    options = [
        TimeOption(
            day=cell.day,  # type: ignore[arg-type]
            start=cell.start,
            end=cell.end,
            week_offset=cell.week_offset,
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
        for cell in best_by_slot.values()
    ]

    # Mejor cobertura primero; ante empate, semana mas cercana, dia y hora.
    return sorted(
        options,
        key=lambda option: (-option.score, option.week_offset, WEEKDAYS.index(option.day), option.start),
    )[:3]


def build_availability_matrix(session: Session) -> list[AvailabilityCell]:
    participant_names = [participant.name for participant in session.participants]
    expected_participants = max(
        len(participant_names),
        session.channel_config.group_participant_count or 0,
        len(session.channel_config.group_participant_ids),
        1,
    )
    workday_start = session.channel_config.workday_start_hour
    workday_end = session.channel_config.workday_end_hour
    scores: dict[tuple[int, str, str, str], set[str]] = {}
    week_offsets: set[int] = {0}  # la semana actual siempre se muestra

    for participant in session.participants:
        for slot in participant.availability:
            week_offsets.add(slot.week_offset)
            for block in split_into_hour_blocks(slot):
                key = (block.week_offset, block.day, block.start, block.end)
                scores.setdefault(key, set()).add(participant.name)

    matrix: list[AvailabilityCell] = []
    for week_offset in sorted(week_offsets):
        for day in WEEKDAYS:
            for hour in range(workday_start, workday_end):
                start = f"{hour:02d}:00"
                end = f"{hour + 1:02d}:00"
                names = scores.get((week_offset, day, start, end), set())
                available = sorted(names)
                unavailable = sorted(name for name in participant_names if name not in names)
                coverage_percent = round((len(available) / expected_participants) * 100)
                matrix.append(
                    AvailabilityCell(
                        day=day,  # type: ignore[arg-type]
                        start=start,
                        end=end,
                        week_offset=week_offset,
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

    expected_participants = max(
        session.channel_config.group_participant_count or 0,
        len(session.channel_config.group_participant_ids),
    )
    unidentified = max(expected_participants - len(session.participants), 0)
    if unidentified:
        missing.append(
            f"Falta identificar la disponibilidad de {unidentified} integrante(s) del grupo."
        )

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
    if not unavailable and coverage_percent == 100:
        return f"Todos los participantes pueden asistir. Cobertura {coverage_percent}%."

    if not unavailable:
        return (
            f"Asisten {len(available)} participante(s): {', '.join(available)}. "
            f"Quedan integrantes del grupo sin disponibilidad identificada. Cobertura {coverage_percent}%."
        )

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
                week_offset=slot.week_offset,
            )
        )
        current = next_time

    return blocks


def parse_time(value: str) -> datetime:
    return datetime.strptime(value, "%H:%M")
