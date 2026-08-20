"""Renderiza la disponibilidad como PNG liviano para WhatsApp.

La grilla se adapta al rango real de horas de la sesion (no solo 09-17) y al
alto necesario para que todas las filas quepan sin recortarse. El layout
imita un horario/planilla real (columnas de dia, filas de hora, bordes finos)
en vez de tarjetas tipo dashboard, para que se lea como un calendario, no
como una pantalla de metricas.
"""
from __future__ import annotations

import base64
import io
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont

from app.schemas import Session, TimeOption
from app.services.calendar_export import (
    date_for_day,
    format_long_date,
    format_short_date,
    option_event_date,
)

WEEKDAYS = ["lunes", "martes", "miercoles", "jueves", "viernes"]
DAY_LABELS = {
    "lunes": "Lunes",
    "martes": "Martes",
    "miercoles": "Miercoles",
    "jueves": "Jueves",
    "viernes": "Viernes",
}

WIDTH = 1125
MARGIN = 32
MAX_HEIGHT = 1600
MIN_CELL_H = 20
PREFERRED_CELL_H = 31

# Presupuesto de alto fijo (todo menos las filas de hora, que son variables).
HEADER_H = 110
HEADER_GAP = 24
TABLE_HEAD_H = 42
TABLE_GAP = 20
SUMMARY_H = 232
FOOTER_H = 44
BOTTOM_MARGIN = 24
HOUR_COL_W = 64

INK = (28, 33, 39)
NAVY = (17, 35, 56)
TEAL = (22, 106, 98)
GREEN = (47, 133, 90)
AMBER = (176, 121, 32)
RED = (178, 66, 66)
MUTED = (110, 119, 128)
SOFT = (247, 248, 249)
CARD = (255, 255, 255)
GRID = (223, 227, 231)
GRID_STRONG = (196, 203, 209)
WHITE = (255, 255, 255)

_FONT_PATHS = {
    False: ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "DejaVuSans.ttf"],
    True: ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "DejaVuSans-Bold.ttf"],
}


def _font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    for path in _FONT_PATHS[bold]:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


def hours_for_session(session: Session) -> list[int]:
    """Horas a dibujar: union de la matriz real y la ventana configurada."""
    hours: set[int] = set()
    for cell in session.availability_matrix:
        try:
            hours.add(int(str(cell.start).split(":")[0]))
        except (TypeError, ValueError):
            continue
    for participant in session.participants:
        for slot in participant.availability:
            try:
                start_h = int(str(slot.start).split(":")[0])
                end_h = int(str(slot.end).split(":")[0])
            except (TypeError, ValueError):
                continue
            for hour in range(start_h, max(start_h + 1, end_h)):
                if 0 <= hour <= 22:
                    hours.add(hour)

    if hours:
        low = min(min(hours), session.channel_config.workday_start_hour)
        high = max(max(hours), session.channel_config.workday_end_hour - 1)
        low = max(0, min(low, 22))
        high = max(low, min(high, 22))
        return list(range(low, high + 1))

    start = max(0, min(session.channel_config.workday_start_hour, 22))
    end = max(start + 1, min(session.channel_config.workday_end_hour, 23))
    return list(range(start, end))


def layout_for_hours(hour_count: int) -> dict[str, int]:
    """Calcula alto de imagen y celda para que la grilla no se recorte."""
    fixed_budget = HEADER_H + HEADER_GAP + TABLE_HEAD_H + TABLE_GAP + SUMMARY_H + FOOTER_H + BOTTOM_MARGIN
    n = max(hour_count, 1)
    available_for_rows = MAX_HEIGHT - fixed_budget
    cell_h = min(PREFERRED_CELL_H, max(MIN_CELL_H, available_for_rows // n))

    table_top = HEADER_H + HEADER_GAP
    table_head_bottom = table_top + TABLE_HEAD_H
    table_bottom = table_head_bottom + n * cell_h
    summary_top = table_bottom + TABLE_GAP
    height = min(MAX_HEIGHT, summary_top + SUMMARY_H + FOOTER_H + BOTTOM_MARGIN)

    return {
        "height": height,
        "cell_h": cell_h,
        "table_top": table_top,
        "table_head_bottom": table_head_bottom,
        "table_bottom": table_bottom,
        "summary_top": summary_top,
        "footer_y": height - FOOTER_H - 6,
    }


def render_availability_png(session: Session, now: datetime | None = None) -> bytes:
    total = max(len(session.participants), 1)
    declared_count = session.channel_config.group_participant_count or len(session.participants)
    best = session.options[0] if session.options else None
    # La grilla muestra una sola semana: la de la mejor opcion (o la actual si aun
    # no hay opciones). Filtrar por week_offset evita que dos semanas colisionen
    # en la misma celda (day, start).
    target_week = best.week_offset if best else 0
    cells = {
        (cell.day, cell.start): cell
        for cell in session.availability_matrix
        if cell.week_offset == target_week
    }
    day_labels = build_day_labels(now, target_week)
    hours = hours_for_session(session)
    layout = layout_for_hours(len(hours))
    height = layout["height"]
    required_names = {participant.name.strip().casefold() for participant in session.participants if participant.required}

    image = Image.new("RGB", (WIDTH, height), CARD)
    draw = ImageDraw.Draw(image)

    fonts = {
        "brand": _font(13, True),
        "title": _font(27, True),
        "meta": _font(14),
        "meta_bold": _font(14, True),
        "table_head": _font(15, True),
        "table_date": _font(12),
        "tag": _font(11, True),
        "hour": _font(13),
        "cell": _font(13 if layout["cell_h"] < 26 else 14, True),
        "summary_label": _font(12, True),
        "summary_title": _font(25, True),
        "summary_sub": _font(15),
        "summary_body": _font(15),
        "small": _font(13),
    }

    draw_header(draw, session, declared_count, fonts)
    draw_schedule_table(
        draw,
        (MARGIN, layout["table_top"], WIDTH - MARGIN, layout["table_bottom"]),
        cells,
        total,
        best,
        fonts,
        day_labels,
        hours=hours,
        cell_h=layout["cell_h"],
        head_h=TABLE_HEAD_H,
        required_names=required_names,
    )
    draw_summary(
        draw,
        (MARGIN, layout["summary_top"], WIDTH - MARGIN, layout["summary_top"] + SUMMARY_H),
        session,
        best,
        fonts,
        now,
    )
    draw_footer(draw, session, fonts, y=layout["footer_y"])

    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def draw_header(draw: ImageDraw.ImageDraw, session: Session, declared_count: int, fonts: dict) -> None:
    draw.rectangle([0, 0, WIDTH, HEADER_H], fill=NAVY)
    draw.text((MARGIN, 22), "C O O R D I N A", font=fonts["brand"], fill=(150, 199, 190))
    title = session.channel_config.group_name or session.title or "Grupo de WhatsApp"
    draw_ellipsis(draw, (MARGIN, 44), title, fonts["title"], WHITE, max_width=680)

    status = "Calendario de disponibilidad"
    if session.options:
        status = "Decision sugerida para la semana"
    draw_right_aligned(draw, (WIDTH - MARGIN, 30), status, fonts["meta_bold"], (215, 226, 233))
    if declared_count:
        draw_right_aligned(
            draw,
            (WIDTH - MARGIN, 52),
            f"{declared_count} integrantes en el grupo",
            fonts["meta"],
            (163, 178, 190),
        )


def build_day_labels(now: datetime | None = None, week_offset: int = 0) -> dict[str, str]:
    """Encabezados con fecha concreta ("Lunes 13/07") para eliminar ambiguedad.

    week_offset selecciona la semana calendario a etiquetar (0 = esta semana)."""
    try:
        return {
            day: format_short_date(date_for_day(day, week_offset, now))
            for day in WEEKDAYS
        }
    except Exception:  # la fecha es opcional: nunca debe romper el render
        return {day: "" for day in WEEKDAYS}


def draw_schedule_table(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    cells: dict,
    total: int,
    best: TimeOption | None,
    fonts: dict,
    day_dates: dict[str, str] | None = None,
    hours: list[int] | None = None,
    cell_h: int = PREFERRED_CELL_H,
    head_h: int = TABLE_HEAD_H,
    required_names: set[str] | None = None,
) -> None:
    x0, y0, x1, y1 = box
    dates = day_dates or {day: "" for day in WEEKDAYS}
    hour_rows = hours if hours is not None else list(range(9, 18))

    grid_x = x0 + HOUR_COL_W
    day_w = (x1 - grid_x) / len(WEEKDAYS)
    head_y1 = y0 + head_h

    # Fila de encabezado (dia + fecha), con la mejor columna resaltada.
    draw.rectangle([x0, y0, x1, head_y1], fill=SOFT, outline=GRID_STRONG, width=1)
    for i, day in enumerate(WEEKDAYS):
        dx0 = grid_x + i * day_w
        dx1 = dx0 + day_w
        is_best_day = bool(best and best.day == day)
        if is_best_day:
            draw.rectangle([dx0, y0, dx1, head_y1], fill=(224, 238, 235))
        draw_centered(draw, (dx0, y0, dx1, y0 + 24), DAY_LABELS[day], fonts["table_head"], INK)
        if dates.get(day):
            draw_centered(draw, (dx0, y0 + 22, dx1, head_y1), dates[day], fonts["table_date"], MUTED)
        if i > 0:
            draw.line([(dx0, y0), (dx0, head_y1)], fill=GRID_STRONG, width=1)

    if best:
        best_x0 = grid_x + WEEKDAYS.index(best.day) * day_w
        draw.rectangle([best_x0, y0 - 18, best_x0 + 60, y0 - 3], fill=TEAL)
        draw_centered(draw, (best_x0, y0 - 19, best_x0 + 60, y0 - 2), "MEJOR", fonts["tag"], WHITE)

    # Filas de hora.
    for row, hour in enumerate(hour_rows):
        ry0 = head_y1 + row * cell_h
        ry1 = ry0 + cell_h
        if ry1 > y1:
            break
        draw.rectangle([x0, ry0, grid_x, ry1], fill=SOFT, outline=GRID, width=1)
        draw.text(
            (x0 + 12, ry0 + max(2, (cell_h - 14) // 2)),
            f"{hour:02d}:00",
            font=fonts["hour"],
            fill=MUTED,
        )
        for i, day in enumerate(WEEKDAYS):
            cx0 = grid_x + i * day_w
            cx1 = cx0 + day_w
            cell = cells.get((day, f"{hour:02d}:00"))
            pct = cell.coverage_percent if cell else 0
            score = cell.score if cell else 0
            is_best = bool(best and best.day == day and best.start == f"{hour:02d}:00")
            draw.rectangle([cx0, ry0, cx1, ry1], fill=coverage_color(pct), outline=GRID, width=1)
            if is_best:
                draw.rectangle([cx0, ry0, cx1, ry1], outline=TEAL, width=2)
            label = f"{score}/{total}"
            if required_names and cell:
                cell_required_names = {
                    name.strip().casefold() for name in cell.available_participants
                }
                required_available_count = len(required_names.intersection(cell_required_names))
                if required_available_count:
                    label += " " + ("★" * required_available_count)
            draw_centered(draw, (cx0, ry0, cx1, ry1), label, fonts["cell"], cell_text_color(pct))

    outer_bottom = min(head_y1 + len(hour_rows) * cell_h, y1)
    draw.rectangle([x0, y0, x1, outer_bottom], outline=GRID_STRONG, width=1)
    draw_heat_legend(draw, x1 - 250, y1 + 6, fonts)


def draw_summary(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    session: Session,
    best: TimeOption | None,
    fonts: dict,
    now: datetime | None = None,
) -> None:
    x0, y0, x1, y1 = box
    draw.line([(x0, y0), (x1, y0)], fill=GRID_STRONG, width=1)
    top = y0 + 22
    required_names = {participant.name.strip().casefold() for participant in session.participants if participant.required}

    if not best:
        draw.text((x0, top), "RESUMEN", font=fonts["summary_label"], fill=MUTED)
        draw.text((x0, top + 22), "Aun faltan datos para sugerir un horario.", font=fonts["summary_body"], fill=INK)
        draw_missing(draw, (x0, top + 60, x1, y1), session, fonts)
        return

    left_w = int((x1 - x0) * 0.52)

    draw.text((x0, top), "MEJOR HORARIO", font=fonts["summary_label"], fill=MUTED)
    day_time = f"{best.day.capitalize()} {best.start}-{best.end}"
    draw_ellipsis(draw, (x0, top + 20), day_time, fonts["summary_title"], INK, max_width=left_w - 12)

    subtitle = f"{best.coverage_percent}% de cobertura"
    try:  # fecha concreta para desambiguar la semana; nunca debe romper el render
        subtitle += f" · {format_long_date(option_event_date(best, now))}"
    except Exception:
        pass
    draw.text((x0, top + 56), subtitle, font=fonts["summary_sub"], fill=coverage_accent(best.coverage_percent))

    if session.missing_info:
        draw_missing(draw, (x0, top + 92, left_w + x0, y1), session, fonts)
    else:
        draw.text((x0, top + 92), "✓ Lista para confirmar en el grupo.", font=fonts["summary_sub"], fill=GREEN)

    right_x = x0 + left_w + 24
    right_w = x1 - right_x
    available = compact_names(mark_required_names(best.available_participants, required_names), max_items=6)
    unavailable = compact_names(mark_required_names(best.unavailable_participants, required_names), max_items=4)

    draw_label_row(draw, right_x, top, right_w, "ASISTEN", available or "Sin asistentes claros", GREEN, fonts)
    if best.unavailable_participants:
        draw_label_row(draw, right_x, top + 66, right_w, "NO CALZAN", unavailable, RED, fonts)
    else:
        draw_label_row(draw, right_x, top + 66, right_w, "CONFLICTOS", "Ninguno detectado", TEAL, fonts)
    if required_names:
        draw.text((right_x, top + 132), "★ obligatorio", font=fonts["table_date"], fill=AMBER)


def draw_footer(draw: ImageDraw.ImageDraw, session: Session, fonts: dict, y: int = 548) -> None:
    trigger = session.channel_config.trigger_word or "@coordina"
    text = f"Para actualizar: escriban nuevas disponibilidades y usen {trigger}."
    draw.line([(MARGIN, y - 10), (WIDTH - MARGIN, y - 10)], fill=GRID, width=1)
    draw_ellipsis(draw, (MARGIN, y), text, fonts["small"], MUTED, max_width=WIDTH - 2 * MARGIN)


def draw_label_row(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    max_width: int,
    label: str,
    value: str,
    accent: tuple[int, int, int],
    fonts: dict,
) -> None:
    draw.text((x, y), label, font=fonts["summary_label"], fill=accent)
    draw_wrapped(draw, value, x, y + 20, max_width, fonts["summary_body"], INK, max_lines=2)


def draw_heat_legend(draw: ImageDraw.ImageDraw, x: int, y: int, fonts: dict) -> None:
    draw.text((x, y + 2), "Baja", font=fonts["table_date"], fill=MUTED)
    lx = x + 38
    for index, pct in enumerate([0, 25, 50, 75, 100]):
        px = lx + index * 27
        draw.rectangle([px, y, px + 18, y + 12], fill=coverage_color(pct), outline=GRID, width=1)
    draw.text((lx + 143, y + 2), "Alta", font=fonts["table_date"], fill=MUTED)


def draw_missing(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], session: Session, fonts: dict) -> None:
    x0, y0, x1, _ = box
    draw.text((x0, y0), "FALTA INFORMACION", font=fonts["summary_label"], fill=AMBER)
    items = session.missing_info[:2] or ["Pidan mas disponibilidades al grupo."]
    y = y0 + 20
    for item in items:
        draw_wrapped(draw, item, x0, y, x1 - x0, fonts["small"], MUTED, max_lines=2)
        y += 34


def draw_centered(draw: ImageDraw.ImageDraw, box, text: str, font, fill) -> None:
    x0, y0, x1, y1 = box
    bbox = draw.textbbox((0, 0), text, font=font)
    width = bbox[2] - bbox[0]
    height = bbox[3] - bbox[1]
    draw.text((x0 + (x1 - x0 - width) / 2, y0 + (y1 - y0 - height) / 2 - 1), text, font=font, fill=fill)


def draw_right_aligned(draw: ImageDraw.ImageDraw, position: tuple[int, int], text: str, font, fill) -> None:
    x, y = position
    width = draw.textlength(text, font=font)
    draw.text((x - width, y), text, font=font, fill=fill)


def draw_ellipsis(
    draw: ImageDraw.ImageDraw,
    position: tuple[int, int],
    text: str,
    font: ImageFont.ImageFont,
    fill: tuple[int, int, int],
    max_width: int,
) -> None:
    value = text
    if draw.textlength(value, font=font) > max_width:
        while value and draw.textlength(value + "...", font=font) > max_width:
            value = value[:-1]
        value = value.rstrip() + "..."
    draw.text(position, value, font=font, fill=fill)


def draw_wrapped(
    draw: ImageDraw.ImageDraw,
    text: str,
    x: int,
    y: int,
    max_width: int,
    font: ImageFont.ImageFont,
    fill: tuple[int, int, int],
    max_lines: int = 2,
) -> None:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if draw.textlength(candidate, font=font) <= max_width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
        if len(lines) == max_lines:
            break
    if current and len(lines) < max_lines:
        lines.append(current)

    for index, line in enumerate(lines[:max_lines]):
        if index == max_lines - 1 and len(lines) == max_lines and " ".join(words) != " ".join(lines):
            while line and draw.textlength(line + "...", font=font) > max_width:
                line = line[:-1]
            line = line.rstrip() + "..."
        draw.text((x, y + index * 19), line, font=font, fill=fill)


def compact_names(names: list[str], max_items: int = 4) -> str:
    if not names:
        return ""
    shown = names[:max_items]
    suffix = ""
    if len(names) > max_items:
        suffix = f" +{len(names) - max_items}"
    return ", ".join(shown) + suffix


def mark_required_names(names: list[str], required_names: set[str]) -> list[str]:
    """Marca visualmente los nombres obligatorios en el resumen del calendario."""
    if not required_names:
        return names
    return [
        f"{name} ★" if name.strip().casefold() in required_names else name
        for name in names
    ]


def coverage_color(percent: int) -> tuple[int, int, int]:
    if percent <= 0:
        return (240, 242, 244)
    if percent < 50:
        return interpolate((250, 240, 222), AMBER, percent / 50)
    return interpolate((228, 240, 234), GREEN, (percent - 50) / 50)


def cell_text_color(percent: int) -> tuple[int, int, int]:
    return WHITE if percent >= 70 else INK


def coverage_accent(percent: int) -> tuple[int, int, int]:
    if percent >= 70:
        return GREEN
    if percent >= 40:
        return AMBER
    return RED


def interpolate(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    amount = min(max(t, 0), 1)
    return tuple(round(a[i] + (b[i] - a[i]) * amount) for i in range(3))


def render_availability_base64(session: Session, now: datetime | None = None) -> str:
    return base64.b64encode(render_availability_png(session, now)).decode("ascii")
