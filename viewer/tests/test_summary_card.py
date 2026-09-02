import io

from PIL import Image

from app import summary_card


def test_content_model_is_the_single_complete_summary_projection():
    raw = {
        "one_line_overview": "Один итог",
        "themes": [{"title": "Тема", "points": ["Первое", "Второе"]}],
        "key_facts": [{"value": "42", "label": "факта"}],
        "decisions": ["Решение"],
        "risks": ["Риск"],
        "action_items": [{"task": "Старый текст"}],
    }
    tasks = [{"id": "t1", "text": "Текущая задача", "owner": "Аня",
              "due": "Завтра", "completed": True}]

    assert summary_card.content_model(raw, tasks) == {
        "overview": "Один итог",
        "themes": [{"title": "Тема", "detail": "Первое · Второе"}],
        "facts": [{"value": "42", "label": "факта"}],
        "decisions": ["Решение"],
        "risks": ["Риск"],
        "tasks": tasks,
    }


def test_summary_card_has_a_white_iphone_sensor_safe_area():
    output = io.BytesIO()
    summary_card.render_summary_card({}, "Проверка", output)
    output.seek(0)

    image = Image.open(output).convert("RGB")
    assert image.width == summary_card.WIDTH
    assert image.height >= summary_card.CONTENT_HEIGHT + summary_card.SAFE_AREA_TOP

    safe_area = image.crop((0, 0, summary_card.WIDTH, summary_card.SAFE_AREA_TOP))
    assert safe_area.getextrema() == ((255, 255), (255, 255), (255, 255))

    # The existing card is preserved rather than squeezed or painted over.
    assert image.getpixel((0, summary_card.SAFE_AREA_TOP)) == (245, 243, 239)


def test_summary_card_grows_to_keep_every_canonical_task_visible():
    output = io.BytesIO()
    data = {
        "overview": "Полное резюме без обрезки",
        "themes": [], "facts": [], "decisions": [], "risks": [],
        "tasks": [
            {"id": str(i), "text": ("Очень подробная задача " * 12) + str(i),
             "owner": "Ответственный", "due": "Завтра", "completed": False}
            for i in range(8)
        ],
    }
    summary_card.render_summary_card(data, "Проверка", output)
    output.seek(0)

    image = Image.open(output)
    assert image.height > summary_card.CONTENT_HEIGHT + summary_card.SAFE_AREA_TOP
