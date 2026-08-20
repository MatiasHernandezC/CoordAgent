from datetime import date, datetime, time, timedelta, timezone, tzinfo
from urllib.parse import urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.schemas import CalendarEventSnapshot, Session, TimeOption
from app.settings import settings

DAY_INDEX = {
    "lunes": 0,
    "martes": 1,
    "miercoles": 2,
    "jueves": 3,
    "viernes": 4,
}

MONTHS = [
    "enero",
    "febrero",
    "marzo",
    "abril",
    "mayo",
    "junio",
    "julio",
    "agosto",
    "septiembre",
    "octubre",
    "noviembre",
    "diciembre",
]


def now_local(now: datetime | None = None) -> datetime:
    tz = load_timezone(settings.app_timezone or "America/Santiago")
    return now.astimezone(tz) if now else datetime.now(tz)


def date_for_day(day: str, week_offset: int = 0, now: datetime | None = None) -> date:
    """Fecha concreta de un dia habil anclada a la semana calendario.

    week_offset se cuenta desde la semana que contiene hoy (lunes como inicio):
    0 = esta semana, 1 = la proxima, N = N semanas adelante. Solo la semana
    actual (offset 0) puede caer en el pasado; en ese caso se corre a la proxima
    ocurrencia para que nunca se agende un dia ya transcurrido."""
    current = now_local(now)
    week_offset = max(0, week_offset)
    monday = (current - timedelta(days=current.weekday())).date()
    candidate = monday + timedelta(days=7 * week_offset + DAY_INDEX[day])
    if week_offset == 0 and candidate < current.date():
        candidate += timedelta(days=7)
    return candidate


def next_date_for_day(day: str, now: datetime | None = None) -> date:
    """Fecha de la proxima ocurrencia del dia habil (hoy cuenta como hoy)."""
    return date_for_day(day, 0, now)


def option_event_date(option: TimeOption, now: datetime | None = None) -> date:
    """Fecha real del evento para una opcion, consistente con el .ics:
    si la hora de hoy ya paso, corre a la proxima semana."""
    current = now_local(now)
    start_at, _ = event_datetimes(option, current, current.tzinfo)
    return start_at.date()


def format_short_date(value: date) -> str:
    return f"{value.day:02d}/{value.month:02d}"


def format_long_date(value: date) -> str:
    return f"{value.day} de {MONTHS[value.month - 1]}"


def event_title(session: Session) -> str:
    """Titulo del evento con referencia explicita al grupo de origen."""
    group_name = session.channel_config.group_name
    if group_name:
        return f"Reunion de {group_name}"
    return session.title or "Reunion de coordinacion"


def event_description(session: Session, option: TimeOption) -> str:
    group_name = session.channel_config.group_name or session.title or "el grupo"
    available = ", ".join(option.available_participants) or "por confirmar"
    unavailable = ", ".join(option.unavailable_participants)
    lines = [
        f"Coordinado automaticamente por Coordina para {group_name}.",
        f"Asisten: {available}.",
    ]
    if unavailable:
        lines.append(f"No calzan: {unavailable}.")
    lines.append(f"Cobertura: {option.coverage_percent}%.")
    return "\n".join(lines)


def build_google_calendar_url(session: Session, now: datetime | None = None) -> str:
    """Link 'Agregar a Google Calendar' clickeable directo desde WhatsApp."""
    option = session.selected_option
    if option is None:
        return ""

    snapshot = calendar_event_snapshot(session, now)
    tz_name = snapshot.timezone
    start_at, end_at = session_event_datetimes(session, now)

    params = {
        "action": "TEMPLATE",
        "text": event_title(session),
        "dates": f"{start_at.strftime('%Y%m%dT%H%M%S')}/{end_at.strftime('%Y%m%dT%H%M%S')}",
        "ctz": tz_name,
        "details": event_description(session, option),
    }
    return "https://calendar.google.com/calendar/render?" + urlencode(params)


def build_calendar_ics(session: Session, now: datetime | None = None) -> str:
    option = session.selected_option
    if option is None:
        raise ValueError("La sesion no tiene una decision confirmada.")

    snapshot = calendar_event_snapshot(session, now)
    tz_name = snapshot.timezone
    start_at, end_at = session_event_datetimes(session, now)

    description = event_description(session, option)

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Coordina//Coordinador Simple MVP//ES",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:{escape_text(snapshot.uid)}",
        f"DTSTAMP:{parse_datetime(snapshot.dtstamp).astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
        f"DTSTART;TZID={tz_name}:{start_at.strftime('%Y%m%dT%H%M%S')}",
        f"DTEND;TZID={tz_name}:{end_at.strftime('%Y%m%dT%H%M%S')}",
        f"SUMMARY:{escape_text(event_title(session))}",
        f"DESCRIPTION:{escape_text(description)}",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return "\r\n".join(fold_ics_line(line) for line in lines) + "\r\n"


def confirmed_event_date(session: Session, now: datetime | None = None) -> date:
    """Devuelve la fecha fijada al confirmar, con fallback para sesiones antiguas."""
    snapshot = calendar_event_snapshot(session, now)
    return parse_datetime(snapshot.start_at).date()


def calendar_event_snapshot(session: Session, now: datetime | None = None) -> CalendarEventSnapshot:
    """Resuelve un snapshot persistido; migra sesiones antiguas de forma estable."""
    if session.selected_calendar_event:
        return session.selected_calendar_event

    for record in reversed(session.decision_history):
        if record.calendar_event:
            return record.calendar_event

    if session.selected_option is None:
        raise ValueError("La sesion no tiene una decision confirmada.")

    option = session.selected_option
    anchor = now_local(now)
    dtstamp = datetime.now(timezone.utc)
    if session.decision_history:
        try:
            dtstamp = parse_datetime(session.decision_history[-1].created_at).astimezone(timezone.utc)
            anchor = now_local(dtstamp)
        except ValueError:
            pass

    tz_name = settings.app_timezone or "America/Santiago"
    tz = load_timezone(tz_name)

    if session.selected_event_date:
        try:
            event_date = date.fromisoformat(session.selected_event_date)
            start_at = datetime.combine(event_date, parse_clock(option.start), tzinfo=tz)
            end_at = datetime.combine(event_date, parse_clock(option.end), tzinfo=tz)
            if end_at <= start_at:
                end_at += timedelta(days=1)
            return CalendarEventSnapshot(
                uid=f"{session.id}-{option.id}@coordina",
                start_at=start_at.isoformat(),
                end_at=end_at.isoformat(),
                timezone=tz_name,
                dtstamp=dtstamp.isoformat(),
            )
        except ValueError:
            pass

    start_at, end_at = event_datetimes(option, anchor, tz)
    return CalendarEventSnapshot(
        uid=f"{session.id}-{option.id}@coordina",
        start_at=start_at.isoformat(),
        end_at=end_at.isoformat(),
        timezone=tz_name,
        dtstamp=dtstamp.isoformat(),
    )


def create_calendar_event_snapshot(
    session: Session,
    option: TimeOption,
    now: datetime | None = None,
) -> CalendarEventSnapshot:
    tz_name = settings.app_timezone or "America/Santiago"
    tz = load_timezone(tz_name)
    current = now_local(now)
    start_at, end_at = event_datetimes(option, current, tz)
    dtstamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return CalendarEventSnapshot(
        uid=f"{session.id}-{option.id}@coordina",
        start_at=start_at.isoformat(),
        end_at=end_at.isoformat(),
        timezone=tz_name,
        dtstamp=dtstamp.isoformat(),
    )


def session_event_datetimes(session: Session, now: datetime | None = None) -> tuple[datetime, datetime]:
    snapshot = calendar_event_snapshot(session, now)
    tz = load_timezone(snapshot.timezone)
    return (
        parse_datetime(snapshot.start_at).astimezone(tz),
        parse_datetime(snapshot.end_at).astimezone(tz),
    )


def parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def event_datetimes(option: TimeOption, now_local: datetime, tz: tzinfo) -> tuple[datetime, datetime]:
    start_time = parse_clock(option.start)
    end_time = parse_clock(option.end)
    week_offset = max(0, getattr(option, "week_offset", 0) or 0)

    # Ancla a la semana calendario (lunes como inicio) y desplaza week_offset
    # semanas. Asi "el martes de la otra semana" cae en el martes correcto sin
    # sobre-desplazarse cuando ese dia ya paso en la semana actual.
    monday = (now_local - timedelta(days=now_local.weekday())).date()
    candidate_date = monday + timedelta(days=7 * week_offset + DAY_INDEX[option.day])
    start_at = datetime.combine(candidate_date, start_time, tzinfo=tz)

    # Solo la semana actual puede quedar en el pasado; corre a la proxima ocurrencia.
    if week_offset == 0 and start_at <= now_local:
        start_at += timedelta(days=7)

    end_at = datetime.combine(start_at.date(), end_time, tzinfo=tz)
    if end_at <= start_at:
        end_at += timedelta(days=1)

    return start_at, end_at


def parse_clock(value: str) -> time:
    hour, minute = value.split(":")
    return time(hour=int(hour), minute=int(minute))


def load_timezone(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return timezone.utc


def escape_text(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace(";", "\\;")
        .replace(",", "\\,")
    )


def fold_ics_line(line: str, limit: int = 75) -> str:
    """Pliega por octetos UTF-8, como exige RFC 5545 (no por caracteres)."""
    chunks: list[str] = []
    current = ""

    for char in line:
        if len((current + char).encode("utf-8")) <= limit:
            current += char
            continue

        if current:
            chunks.append(current)
        current = " " + char

    if current or not chunks:
        chunks.append(current)
    return "\r\n".join(chunks)
