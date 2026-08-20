from datetime import datetime, timedelta

from app.schemas import AvailabilityCell, Participant, Session, TimeOption, TimeSlot

WEEKDAYS = ["lunes", "martes", "miercoles", "jueves", "viernes"]
WORKDAY_START = 9
WORKDAY_END = 18


def calculate_options(session: Session) -> list[TimeOption]:
    return options_from_matrix(build_availability_matrix(session))


def _requirement_profile(session: Session) -> tuple[dict[str, int], set[str]]:
    """(peso por participante, nombres requeridos). Peso 0/ausente -> 1."""
    weights: dict[str, int] = {}
    required: set[str] = set()
    for participant in session.participants:
        weight = int(participant.priority or 0)
        weights[participant.name] = weight if weight > 0 else 1
        if participant.required:
            required.add(participant.name)
    return weights, required


def options_from_matrix(matrix: list[AvailabilityCell]) -> list[TimeOption]:
    # Diversifica: un solo bloque (el mejor) por (semana, dia). Asi la misma
    # persona en dias/semanas distintos no genera opciones redundantes, y dos
    # semanas no colisionan. Ante empate de score se conserva el mas temprano.
    #
    # Requeridos: si existe al menos una celda que los cubre a todos, SOLO se
    # recomiendan celdas con required_met True. Si ninguna los cubre (fallback),
    # se mantienen las mejores celdas con required_met False para que la
    # respuesta pueda avisar claramente quien queda fuera.
    scorable = [cell for cell in matrix if cell.score > 0]
    if not scorable:
        return []
    # Requeridos: si existe al menos una celda que los cubre a todos, SOLO se
    # recomiendan celdas con required_met True. Si ninguna los cubre (fallback),
    # se mantienen las mejores celdas con required_met False para que la
    # respuesta pueda avisar claramente quien queda fuera.
    if any(cell.required_met for cell in scorable):
        pool = [cell for cell in scorable if cell.required_met]
    else:
        pool = scorable

    best_by_slot: dict[tuple[int, str], AvailabilityCell] = {}
    for cell in pool:
        if cell.score == 0:
            continue
        key = (cell.week_offset, cell.day)
        best = best_by_slot.get(key)
        if best is None or (cell.weighted_score, cell.score) > (best.weighted_score, best.score):
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
                required_missing=cell.required_missing,
                required_met=cell.required_met,
            ),
            weighted_score=cell.weighted_score,
            required_met=cell.required_met,
            required_missing=list(cell.required_missing),
        )
        for cell in best_by_slot.values()
    ]

    # Mejor ponderado primero (con pesos = 1 es identico al score anterior);
    # ante empate, semana mas cercana, dia y hora.
    return sorted(
        options,
        key=lambda option: (-option.weighted_score, option.week_offset, WEEKDAYS.index(option.day), option.start),
    )[:3]


def build_availability_matrix(session: Session) -> list[AvailabilityCell]:
    participant_names = [participant.name for participant in session.participants]
    expected_participants = max(
        len(participant_names),
        session.channel_config.group_participant_count or 0,
        len(session.channel_config.group_participant_ids),
        1,
    )
    weights, required_names = _requirement_profile(session)
    workday_start = session.channel_config.workday_start_hour
    workday_end = session.channel_config.workday_end_hour
    scores: dict[tuple[int, str, str, str], set[str]] = {}
    week_offsets: set[int] = {0}  # la semana actual siempre se muestra

    for participant in session.participants:
        for slot in participant.availability:
            week_offsets.add(slot.week_offset)
            # Expand the matrix window when declared slots fall outside the
            # configured workday (e.g. "a las 7" → 19:00 with end=18).
            try:
                slot_start_h = int(slot.start.split(":")[0])
                slot_end_h = int(slot.end.split(":")[0])
            except (TypeError, ValueError):
                slot_start_h, slot_end_h = workday_start, workday_end
            workday_start = min(workday_start, max(0, slot_start_h))
            workday_end = max(workday_end, min(23, slot_end_h))
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
                required_available = sorted(required_names & names)
                required_missing = sorted(required_names - names)
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
                        weighted_score=sum(weights.get(name, 1) for name in available),
                        required_met=not required_missing,
                        required_missing=required_missing,
                    )
                )

    return matrix


def find_missing_info(session: Session) -> list[str]:
    missing: list[str] = []
    if not session.participants:
        missing.append("Agrega participantes o escribe un mensaje con disponibilidades.")

    identified = 0
    for participant in session.participants:
        if participant.roster_only and not participant.availability and not participant.required:
            # Se conoce el nombre por el padron del canal (Slack/WhatsApp),
            # pero la persona todavia no escribio nada: no se la nombra una
            # por una (seria ruido apenas se vincula el canal), cae en el
            # total generico de "sin identificar" de abajo, igual que antes
            # de que existiera el padron.
            continue
        identified += 1
        if not participant.availability:
            if participant.required:
                missing.append(f"Falta disponibilidad de {participant.name} (participante requerido).")
            else:
                missing.append(f"Falta disponibilidad de {participant.name}.")

    expected_participants = max(
        session.channel_config.group_participant_count or 0,
        len(session.channel_config.group_participant_ids),
    )
    unidentified = max(expected_participants - identified, 0)
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

    required_names = [participant.name for participant in session.participants if participant.required]
    prioritized_names = [participant.name for participant in session.participants if priority_of(participant)]
    if required_names:
        insights.append(f"Participantes requeridos: {', '.join(required_names)}.")
    if prioritized_names:
        insights.append(f"Con prioridad ponderada: {', '.join(prioritized_names)}.")

    if session.missing_info:
        insights.append(f"Hay {len(session.missing_info)} dato(s) faltante(s) antes de una decision ideal.")

    if session.options:
        best = session.options[0]
        insights.append(
            f"La mejor opcion actual cubre {best.coverage_percent}% del grupo: {best.day} {best.start}-{best.end}."
        )
        if required_names:
            if best.required_met:
                insights.append("La mejor opcion incluye a todos los requeridos.")
            else:
                insights.append(f"La mejor opcion NO incluye a: {', '.join(best.required_missing)}.")
        if best.unavailable_participants:
            insights.append(f"Quedan fuera en la mejor opcion: {', '.join(best.unavailable_participants)}.")
        else:
            insights.append("La mejor opcion incluye a todos los participantes.")

    return insights


def priority_of(participant: Participant) -> int:
    return max(int(participant.priority or 0), 0)


def build_explanation(
    available: list[str],
    unavailable: list[str],
    coverage_percent: int,
    *,
    required_missing: list[str] | None = None,
    required_met: bool | None = None,
) -> str:
    if not unavailable and coverage_percent == 100:
        base = f"Todos los participantes pueden asistir. Cobertura {coverage_percent}%."
    elif not unavailable:
        base = (
            f"Asisten {len(available)} participante(s): {', '.join(available)}. "
            f"Quedan integrantes del grupo sin disponibilidad identificada. Cobertura {coverage_percent}%."
        )
    else:
        base = (
            f"Asisten {len(available)} participante(s): {', '.join(available)}. "
            f"No calzan: {', '.join(unavailable)}. Cobertura {coverage_percent}%."
        )
    if required_missing:
        base += f" Faltan requeridos: {', '.join(required_missing)}."
    elif required_met is True:
        base += " Incluye a todos los requeridos."
    return base


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
