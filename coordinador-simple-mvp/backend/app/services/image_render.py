"""Renderiza la disponibilidad como PNG liviano para WhatsApp."""
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
    "lunes": "Lun",
    "martes": "Mar",
    "miercoles": "Mie",
    "jueves": "Jue",
    "viernes": "Vie",
}
HOURS = list(range(9, 18))

WIDTH = 1125
HEIGHT = 600
MARGIN = 32

INK = (20, 35, 49)
NAVY = (18, 48, 74)
TEAL = (28, 122, 112)
GREEN = (68, 156, 111)
AMBER = (219, 161, 65)
RED = (200, 92, 92)
MUTED = (101, 113, 122)
SOFT = (244, 247, 249)
CARD = (255, 255, 255)
GRID = (214, 224, 228)
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

    image = Image.new("RGB", (WIDTH, HEIGHT), SOFT)
    draw = ImageDraw.Draw(image)

    fonts = {
        "brand": _font(18, True),
        "title": _font(30, True),
        "subtitle": _font(17),
        "section": _font(19, True),
        "small": _font(13),
        "label": _font(14, True),
        "metric": _font(46, True),
        "time": _font(30, True),
        "cell": _font(14, True),
        "body": _font(15),
    }

    draw_header(draw, session, declared_count, fonts)

    heatmap_box = (MARGIN, 122, 740, 526)
    insight_box = (766, 122, WIDTH - MARGIN, 526)
    draw_card(draw, heatmap_box, radius=18)
    draw_card(draw, insight_box, radius=18)

    draw_heatmap(draw, heatmap_box, cells, total, best, fonts, day_labels)
    draw_insights(draw, insight_box, session, best, fonts, now)
    draw_footer(draw, session, fonts)

    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def draw_header(draw: ImageDraw.ImageDraw, session: Session, declared_count: int, fonts: dict) -> None:
    draw.rounded_rectangle([MARGIN, 24, WIDTH - MARGIN, 94], radius=22, fill=NAVY)
    draw.text((MARGIN + 28, 42), "Coordina", font=fonts["brand"], fill=(194, 242, 231))
    title = session.channel_config.group_name or session.title or "Grupo de WhatsApp"
    draw_ellipsis(draw, (MARGIN + 28, 62), title, fonts["title"], WHITE, max_width=520)

    status = "Calendario de disponibilidad"
    if session.options:
        status = "Decision sugerida"
    draw_pill(draw, (WIDTH - MARGIN - 300, 42, WIDTH - MARGIN - 28, 76), status, fonts["label"], (220, 244, 238), (25, 76, 76))
    if declared_count:
        draw.text((WIDTH - MARGIN - 300, 80), f"{declared_count} integrantes en el grupo", font=fonts["small"], fill=(202, 220, 227))


def build_day_labels(now: datetime | None = None, week_offset: int = 0) -> dict[str, str]:
    """Encabezados con fecha concreta ("Lun 13/07") para eliminar ambiguedad.

    week_offset selecciona la semana calendario a etiquetar (0 = esta semana)."""
    try:
        return {
            day: f"{DAY_LABELS[day]} {format_short_date(date_for_day(day, week_offset, now))}"
            for day in WEEKDAYS
        }
    except Exception:  # la fecha es opcional: nunca debe romper el render
        return dict(DAY_LABELS)


def draw_heatmap(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    cells: dict,
    total: int,
    best: TimeOption | None,
    fonts: dict,
    day_labels: dict[str, str] | None = None,
) -> None:
    x0, y0, x1, y1 = box
    labels = day_labels or DAY_LABELS
    draw.text((x0 + 24, y0 + 18), "Cobertura por horario", font=fonts["section"], fill=INK)
    draw.text((x0 + 24, y0 + 44), "Cada celda muestra cuantos pueden asistir.", font=fonts["small"], fill=MUTED)
    draw_heat_legend(draw, x1 - 292, y0 + 42, fonts)

    grid_x = x0 + 82
    grid_y = y0 + 80
    day_w = (x1 - grid_x - 24) / len(WEEKDAYS)
    head_h = 34
    cell_h = 31

    for i, day in enumerate(WEEKDAYS):
        dx0 = grid_x + i * day_w
        fill = NAVY if best and best.day == day else (231, 237, 241)
        text_fill = WHITE if best and best.day == day else INK
        draw.rounded_rectangle([dx0 + 3, grid_y, dx0 + day_w - 3, grid_y + head_h], radius=9, fill=fill)
        draw_centered(draw, (dx0, grid_y, dx0 + day_w, grid_y + head_h), labels[day], fonts["label"], text_fill)

    for row, hour in enumerate(HOURS):
        cy = grid_y + head_h + 9 + row * cell_h
        draw.text((x0 + 24, cy + 7), f"{hour:02d}", font=fonts["small"], fill=MUTED)
        for i, day in enumerate(WEEKDAYS):
            cx = grid_x + i * day_w
            cell = cells.get((day, f"{hour:02d}:00"))
            pct = cell.coverage_percent if cell else 0
            score = cell.score if cell else 0
            is_best = best and best.day == day and best.start == f"{hour:02d}:00"
            fill = coverage_color(pct)
            outline = TEAL if is_best else GRID
            width = 3 if is_best else 1
            draw.rounded_rectangle(
                [cx + 4, cy + 3, cx + day_w - 4, cy + cell_h - 3],
                radius=8,
                fill=fill,
                outline=outline,
                width=width,
            )
            label = f"{score}/{total}"
            draw_centered(draw, (cx, cy, cx + day_w, cy + cell_h), label, fonts["cell"], cell_text_color(pct))


def draw_insights(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    session: Session,
    best: TimeOption | None,
    fonts: dict,
    now: datetime | None = None,
) -> None:
    x0, y0, x1, y1 = box
    draw.text((x0 + 24, y0 + 18), "Resumen accionable", font=fonts["section"], fill=INK)

    if not best:
        draw.text((x0 + 24, y0 + 64), "Aun faltan datos para sugerir un horario.", font=fonts["body"], fill=MUTED)
        draw_missing(draw, (x0 + 24, y0 + 112, x1 - 24, y1 - 24), session, fonts)
        return

    coverage = f"{best.coverage_percent}%"
    draw.text((x0 + 24, y0 + 56), coverage, font=fonts["metric"], fill=coverage_accent(best.coverage_percent))
    draw.text((x0 + 24, y0 + 106), "cobertura", font=fonts["label"], fill=MUTED)

    day_time = f"{best.day.capitalize()} {best.start}-{best.end}"
    draw_ellipsis(draw, (x0 + 24, y0 + 144), day_time, fonts["time"], INK, max_width=x1 - x0 - 48)
    subtitle = "Mejor opcion detectada"
    try:  # fecha concreta para desambiguar la semana; nunca debe romper el render
        subtitle = f"Mejor opcion · {format_long_date(option_event_date(best, now))}"
    except Exception:
        pass
    draw.text((x0 + 24, y0 + 182), subtitle, font=fonts["small"], fill=MUTED)

    available = compact_names(best.available_participants, max_items=4)
    unavailable = compact_names(best.unavailable_participants, max_items=3)

    draw_label_block(draw, x0 + 24, y0 + 222, "Asisten", available or "Sin asistentes claros", GREEN, fonts)
    if best.unavailable_participants:
        draw_label_block(draw, x0 + 24, y0 + 288, "No calzan", unavailable, RED, fonts)
    else:
        draw_label_block(draw, x0 + 24, y0 + 288, "Conflictos", "Ninguno detectado", TEAL, fonts)

    if session.missing_info:
        draw_missing(draw, (x0 + 24, y0 + 354, x1 - 24, y1 - 24), session, fonts)
    else:
        draw_pill(draw, (x0 + 24, y1 - 58, x1 - 24, y1 - 24), "Listo para confirmar en el grupo", fonts["label"], (229, 246, 237), GREEN)


def draw_footer(draw: ImageDraw.ImageDraw, session: Session, fonts: dict) -> None:
    y = 548
    trigger = session.channel_config.trigger_word or "@coordina"
    text = f"Para actualizar: escriban nuevas disponibilidades y usen {trigger}."
    draw_ellipsis(draw, (MARGIN + 4, y), text, fonts["small"], MUTED, max_width=WIDTH - 2 * MARGIN - 8)


def draw_label_block(draw: ImageDraw.ImageDraw, x: int, y: int, label: str, value: str, accent: tuple[int, int, int], fonts: dict) -> None:
    draw.text((x, y), label.upper(), font=fonts["small"], fill=accent)
    draw_ellipsis(draw, (x, y + 20), value, fonts["body"], INK, max_width=310)


def draw_heat_legend(draw: ImageDraw.ImageDraw, x: int, y: int, fonts: dict) -> None:
    draw.text((x, y + 1), "Baja", font=fonts["small"], fill=MUTED)
    lx = x + 42
    for index, pct in enumerate([0, 25, 50, 75, 100]):
        px = lx + index * 31
        draw.rounded_rectangle([px, y, px + 22, y + 14], radius=5, fill=coverage_color(pct))
    draw.text((lx + 160, y + 1), "Alta", font=fonts["small"], fill=MUTED)


def draw_missing(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], session: Session, fonts: dict) -> None:
    x0, y0, x1, _ = box
    draw.text((x0, y0), "Falta informacion", font=fonts["label"], fill=AMBER)
    items = session.missing_info[:3] or ["Pidan mas disponibilidades al grupo."]
    y = y0 + 24
    for item in items:
        draw_wrapped(draw, item, x0, y, x1 - x0, fonts["small"], MUTED, max_lines=2)
        y += 36


def draw_card(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], radius: int = 16) -> None:
    x0, y0, x1, y1 = box
    draw.rounded_rectangle([x0 + 4, y0 + 5, x1 + 4, y1 + 5], radius=radius, fill=(225, 231, 235))
    draw.rounded_rectangle(box, radius=radius, fill=CARD, outline=(225, 232, 236), width=1)


def draw_pill(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    text: str,
    font: ImageFont.ImageFont,
    fill: tuple[int, int, int],
    text_fill: tuple[int, int, int],
) -> None:
    draw.rounded_rectangle(box, radius=14, fill=fill)
    draw_centered(draw, box, text, font, text_fill)


def draw_centered(draw: ImageDraw.ImageDraw, box, text: str, font, fill) -> None:
    x0, y0, x1, y1 = box
    bbox = draw.textbbox((0, 0), text, font=font)
    width = bbox[2] - bbox[0]
    height = bbox[3] - bbox[1]
    draw.text((x0 + (x1 - x0 - width) / 2, y0 + (y1 - y0 - height) / 2 - 1), text, font=font, fill=fill)


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
        draw.text((x, y + index * 17), line, font=font, fill=fill)


def compact_names(names: list[str], max_items: int = 4) -> str:
    if not names:
        return ""
    shown = names[:max_items]
    suffix = ""
    if len(names) > max_items:
        suffix = f" +{len(names) - max_items}"
    return ", ".join(shown) + suffix


def coverage_color(percent: int) -> tuple[int, int, int]:
    if percent <= 0:
        return (238, 242, 245)
    if percent < 50:
        return interpolate((247, 238, 220), AMBER, percent / 50)
    return interpolate((226, 242, 232), GREEN, (percent - 50) / 50)


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
