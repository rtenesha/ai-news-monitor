from notifier import _parse_sections, _SECTION_MARKERS


def test_section_markers_are_single_not_numbered():
    assert _SECTION_MARKERS == ["ЗАГОЛОВОК", "ТЕЛО", "CTA"]


def test_parse_sections_single_markers():
    raw = "ЗАГОЛОВОК: Claude научился писать код лучше 🚀\nТЕЛО: Первый абзац.\n\nВторой абзац.\nCTA: Пробовали уже?"
    sections = _parse_sections(raw)
    assert sections["ЗАГОЛОВОК"] == "Claude научился писать код лучше 🚀"
    assert sections["ТЕЛО"] == "Первый абзац.\n\nВторой абзац."
    assert sections["CTA"] == "Пробовали уже?"
