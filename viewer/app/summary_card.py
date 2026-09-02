"""Server-side structured summary card renderer."""
import sys
from pathlib import Path

VENDOR = Path(__file__).parent / "vendor"
if str(VENDOR) not in sys.path:
    sys.path.insert(0, str(VENDOR))

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

WIDTH, CONTENT_HEIGHT = 1080, 1350
SAFE_AREA_TOP = 144
HEIGHT = CONTENT_HEIGHT + SAFE_AREA_TOP
BG = "#F5F3EF"
INK = "#222129"
MUTED = "#6F6B77"
PURPLE = "#6D5BD0"
PALE_PURPLE = "#ECE8FF"
CARD = "#FFFFFF"
BORDER = "#E3DED7"
GREEN = "#267A62"
PALE_GREEN = "#E4F4EE"
BLUE = "#315F9E"
PALE_BLUE = "#E9F1FC"
FONT_DIR = Path(__file__).parent / "fonts"


def _font(size, bold=False):
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    return ImageFont.truetype(str(FONT_DIR / name), size)


def _text(value):
    if value is None:
        return ""
    if isinstance(value, dict):
        return str(value.get("task") or value.get("text") or value.get("summary")
                   or value.get("title") or value.get("value") or "")
    return str(value)


def _items(value):
    return value if isinstance(value, list) else []


def content_model(summary_data, tasks=None):
    """One canonical summary payload shared by the detail UI and PNG."""
    data = summary_data if isinstance(summary_data, dict) else {}

    def text(value):
        return " ".join(_text(value).split())

    themes = []
    for raw in _items(data.get("themes"))[:6]:
        if isinstance(raw, dict):
            title = text(raw.get("title") or raw.get("name") or "Тема")
            detail = text(raw.get("summary") or raw.get("description"))
            if not detail:
                detail = " · ".join(
                    text(item) for item in _items(raw.get("points") or raw.get("key_points"))
                    if text(item)
                )
        else:
            title, detail = "Тема", text(raw)
        if title or detail:
            themes.append({"title": title or "Тема", "detail": detail})

    facts = []
    for raw in _items(data.get("key_facts") or data.get("facts"))[:6]:
        obj = raw if isinstance(raw, dict) else {"value": raw}
        value = text(obj.get("value") or obj.get("number") or obj.get("text") or raw)
        if value:
            facts.append({"value": value, "label": text(obj.get("label"))})

    def strings(value, limit=6):
        result = []
        for item in _items(value)[:limit]:
            value_text = text(item)
            if value_text:
                result.append(value_text)
        return result

    canonical_tasks = []
    for raw in _items(tasks)[:8]:
        if not isinstance(raw, dict):
            continue
        task = {
            "id": str(raw.get("id") or ""),
            "text": text(raw.get("text") or raw.get("task") or raw.get("title")),
            "owner": text(raw.get("owner")) or None,
            "due": text(raw.get("due") or raw.get("deadline")) or None,
            "completed": bool(raw.get("completed")),
        }
        if task["text"]:
            canonical_tasks.append(task)

    return {
        "overview": text(data.get("overview") or data.get("one_line_overview")
                         or data.get("summary") or data.get("brief_summary")),
        "themes": themes,
        "facts": facts,
        "decisions": strings(data.get("decisions")),
        "risks": strings(data.get("risks")),
        "tasks": canonical_tasks,
    }


def _lines(draw, text, font, max_width, max_lines=None):
    words = _text(text).replace("\n", " ").split()
    lines, current = [], ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if not current or draw.textlength(candidate, font=font) <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
            if max_lines and len(lines) >= max_lines:
                break
    if current and (not max_lines or len(lines) < max_lines):
        lines.append(current)
    source = " ".join(words)
    if max_lines and lines and len(" ".join(lines)) < len(source):
        while lines[-1] and draw.textlength(lines[-1] + "…", font=font) > max_width:
            lines[-1] = lines[-1][:-1]
        lines[-1] = lines[-1].rstrip() + "…"
    return lines


def _draw_lines(draw, xy, text, font, fill, max_width, spacing=8, max_lines=None):
    x, y = xy
    lines = _lines(draw, text, font, max_width, max_lines)
    line_height = font.size + spacing
    for line in lines:
        draw.text((x, y), line, font=font, fill=fill)
        y += line_height
    return y


def _theme_parts(theme):
    if isinstance(theme, str):
        return theme, ""
    if not isinstance(theme, dict):
        return "", ""
    title = _text(theme.get("title") or theme.get("name") or "Тема")
    detail = theme.get("summary") or theme.get("description") or ""
    if not detail:
        points = _items(theme.get("points") or theme.get("key_points"))
        detail = _text(points[0]) if points else ""
    return title, _text(detail)


def _fallback_themes(data):
    themes = _items(data.get("themes"))
    if themes:
        return themes[:3]
    facts = _items(data.get("key_facts") or data.get("facts"))
    return [{"title": "Главное", "summary": _text(item)} for item in facts[:3]]


def _bullet_box(draw, box, title, items, color, tint):
    x, y, right, bottom = box
    draw.rounded_rectangle(box, radius=28, fill=CARD, outline=BORDER, width=2)
    draw.rounded_rectangle((x + 24, y + 22, x + 66, y + 64), radius=12, fill=tint)
    draw.ellipse((x + 39, y + 37, x + 51, y + 49), fill=color)
    draw.text((x + 80, y + 24), title, font=_font(25, True), fill=INK)
    cursor = y + 82
    body = _font(19)
    for item in _items(items)[:2]:
        text = _text(item)
        if not text or cursor > bottom - 54:
            continue
        draw.ellipse((x + 28, cursor + 8, x + 38, cursor + 18), fill=color)
        cursor = _draw_lines(draw, (x + 50, cursor), text, body, INK,
                             right - x - 74, 5, 2) + 10


def render_summary_card(summary_data, title, output):
    """Render every field from the canonical detail-summary payload."""
    data = summary_data if isinstance(summary_data, dict) else {}
    # Draw onto a tall scratch surface, then crop to the real content height.
    scratch = Image.new("RGB", (WIDTH, 30000), BG)
    draw = ImageDraw.Draw(scratch)
    left, right = 64, 1016
    y = 52

    draw.rounded_rectangle((left, y, 212, y + 50), radius=25, fill=INK)
    draw.text((89, y + 12), "PLAUD", font=_font(20, True), fill="white")
    draw.text((232, y + 15), "КРАТКОЕ РЕЗЮМЕ", font=_font(17, True), fill=MUTED)
    y += 76
    y = _draw_lines(draw, (left, y), title or "Без названия",
                    _font(42, True), INK, right - left, 8) + 34

    overview = _text(data.get("overview"))
    if overview:
        overview_lines = _lines(draw, overview, _font(23), 896)
        box_height = 96 + len(overview_lines) * 31
        draw.rounded_rectangle((left, y, right, y + box_height), radius=30,
                               fill=PALE_PURPLE)
        draw.text((92, y + 27), "В ДВУХ СЛОВАХ", font=_font(17, True), fill=PURPLE)
        _draw_lines(draw, (92, y + 67), overview, _font(23), INK, 896, 8)
        y += box_height + 34

    themes = _items(data.get("themes"))
    if themes:
        draw.text((left, y), "ГЛАВНЫЕ ТЕМЫ", font=_font(18, True), fill=MUTED)
        y += 38
        for index, theme in enumerate(themes):
            obj = theme if isinstance(theme, dict) else {"title": "Тема", "detail": theme}
            heading = _text(obj.get("title") or "Тема")
            detail = _text(obj.get("detail"))
            heading_lines = _lines(draw, heading, _font(25, True), 820)
            detail_lines = _lines(draw, detail, _font(19), 820) if detail else []
            box_height = max(106, 36 + len(heading_lines) * 30 + len(detail_lines) * 24 + 30)
            draw.rounded_rectangle((left, y, right, y + box_height), radius=28,
                                   fill=CARD, outline=BORDER, width=2)
            draw.rounded_rectangle((88, y + 25, 138, y + 75), radius=16, fill=PALE_BLUE)
            draw.text((105, y + 33), str(index + 1), font=_font(22, True), fill=BLUE)
            text_y = _draw_lines(draw, (160, y + 22), heading, _font(25, True),
                                 INK, 820, 5)
            if detail:
                _draw_lines(draw, (160, text_y + 4), detail, _font(19), MUTED, 820, 5)
            y += box_height + 20
        y += 14

    facts = _items(data.get("facts"))
    if facts:
        draw.text((left, y), "КЛЮЧЕВЫЕ ФАКТЫ", font=_font(18, True), fill=MUTED)
        y += 38
        for fact in facts:
            obj = fact if isinstance(fact, dict) else {"value": fact}
            value, label = _text(obj.get("value")), _text(obj.get("label"))
            value_lines = _lines(draw, value, _font(27, True), 870)
            label_lines = _lines(draw, label, _font(18), 870) if label else []
            box_height = 42 + len(value_lines) * 34 + len(label_lines) * 23
            draw.rounded_rectangle((left, y, right, y + box_height), radius=24,
                                   fill=INK)
            next_y = _draw_lines(draw, (92, y + 21), value, _font(27, True),
                                 "white", 870, 6)
            if label:
                _draw_lines(draw, (92, next_y + 2), label, _font(18), "#C8C5CF", 870, 5)
            y += box_height + 16
        y += 18

    def bullet_section(section_title, items, color, tint):
        nonlocal y
        if not items:
            return
        draw.text((left, y), section_title.upper(), font=_font(18, True), fill=MUTED)
        y += 38
        for item in items:
            text = _text(item)
            lines = _lines(draw, text, _font(21), 850)
            box_height = 42 + len(lines) * 28
            draw.rounded_rectangle((left, y, right, y + box_height), radius=24,
                                   fill=CARD, outline=BORDER, width=2)
            draw.ellipse((92, y + 26, 106, y + 40), fill=color)
            _draw_lines(draw, (126, y + 19), text, _font(21), INK, 850, 7)
            y += box_height + 14
        y += 20

    bullet_section("Решения", _items(data.get("decisions")), GREEN, PALE_GREEN)
    bullet_section("Риски", _items(data.get("risks")), "#AD5656", "#F8E8E8")

    tasks = _items(data.get("tasks"))
    if tasks:
        draw.text((left, y), "ЗАДАЧИ", font=_font(18, True), fill=MUTED)
        y += 38
        for task in tasks:
            obj = task if isinstance(task, dict) else {"text": task}
            text = _text(obj.get("text"))
            meta = " · ".join(str(value) for value in (obj.get("owner"), obj.get("due")) if value)
            text_lines = _lines(draw, text, _font(21, True), 820)
            meta_lines = _lines(draw, meta, _font(17), 820) if meta else []
            box_height = 46 + len(text_lines) * 28 + len(meta_lines) * 22
            draw.rounded_rectangle((left, y, right, y + box_height), radius=24,
                                   fill=PALE_PURPLE, outline=BORDER, width=2)
            draw.rectangle((92, y + 24, 116, y + 48), outline=PURPLE, width=3)
            if obj.get("completed"):
                draw.text((94, y + 20), "✓", font=_font(23, True), fill=PURPLE)
            text_y = _draw_lines(draw, (136, y + 19), text, _font(21, True),
                                 INK, 820, 7)
            if meta:
                _draw_lines(draw, (136, text_y + 2), meta, _font(17), MUTED, 820, 5)
            y += box_height + 14

    content_height = max(CONTENT_HEIGHT, y + 60)
    image = scratch.crop((0, 0, WIDTH, content_height))
    canvas = Image.new("RGB", (WIDTH, content_height + SAFE_AREA_TOP), "#FFFFFF")
    canvas.paste(image, (0, SAFE_AREA_TOP))
    canvas.save(output, format="PNG", optimize=True)
