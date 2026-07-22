from datetime import date, datetime, time, timedelta, timezone, tzinfo
from urllib.parse import urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.schemas import Session, TimeOption
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


def build_google_calendar_url(session: Session, now: datetime | None = None) -> str:
    """Link 'Agregar a Google Calendar' clickeable directo desde WhatsApp."""
    option = session.selected_option
    if option is None:
        return ""

    tz_name = settings.app_timezone or "America/Santiago"
    current = now_local(now)
    start_at, end_at = event_datetimes(option, current, current.tzinfo)

    available = ", ".join(option.available_participants) or "por confirmar"
    params = {
        "action": "TEMPLATE",
        "text": session.channel_config.group_name or session.title or "Reunion",
        "dates": f"{start_at.strftime('%Y%m%dT%H%M%S')}/{end_at.strftime('%Y%m%dT%H%M%S')}",
        "ctz": tz_name,
        "details": f"Coordinado con Coordina. Asisten: {available}.",
    }
    return "https://calendar.google.com/calendar/render?" + urlencode(params)


def build_calendar_ics(session: Session, now: datetime | None = None) -> str:
    option = session.selected_option
    if option is None:
        raise ValueError("La sesion no tiene una decision confirmada.")

    tz_name = settings.app_timezone or "America/Santiago"
    tz = load_timezone(tz_name)
    now_local = now.astimezone(tz) if now else datetime.now(tz)
    start_at, end_at = event_datetimes(option, now_local, tz)

    available = ", ".join(option.available_participants) or "Sin participantes confirmados"
    unavailable = ", ".join(option.unavailable_participants) or "Sin conflictos registrados"
    description = (
        f"Decision confirmada por Coordina.\\n"
        f"Asisten: {available}.\\n"
        f"No calzan: {unavailable}.\\n"
        f"Cobertura: {option.coverage_percent}%."
    )

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Coordina//Coordinador Simple MVP//ES",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:{escape_text(session.id)}-{escape_text(option.id)}@coordina",
        f"DTSTAMP:{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
        f"DTSTART;TZID={tz_name}:{start_at.strftime('%Y%m%dT%H%M%S')}",
        f"DTEND;TZID={tz_name}:{end_at.strftime('%Y%m%dT%H%M%S')}",
        f"SUMMARY:{escape_text(session.channel_config.group_name or session.title)}",
        f"DESCRIPTION:{escape_text(description)}",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return "\r\n".join(fold_ics_line(line) for line in lines) + "\r\n"


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
    if len(line) <= limit:
        return line

    chunks = [line[:limit]]
    rest = line[limit:]
    while rest:
        chunks.append(" " + rest[: limit - 1])
        rest = rest[limit - 1 :]
    return "\r\n".join(chunks)
