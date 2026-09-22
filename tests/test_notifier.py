import os
from unittest import mock
from notifier import _parse_sections, _SECTION_MARKERS, generate_post


def test_section_markers_are_single_not_numbered():
    assert _SECTION_MARKERS == ["ЗАГОЛОВОК", "ТЕЛО", "CTA"]


def test_parse_sections_single_markers():
    raw = "ЗАГОЛОВОК: Claude научился писать код лучше 🚀\nТЕЛО: Первый абзац.\n\nВторой абзац.\nCTA: Пробовали уже?"
    sections = _parse_sections(raw)
    assert sections["ЗАГОЛОВОК"] == "Claude научился писать код лучше 🚀"
    assert sections["ТЕЛО"] == "Первый абзац.\n\nВторой абзац."
    assert sections["CTA"] == "Пробовали уже?"


def test_generate_post_fallback_escapes_html_in_title():
    """Verify that generate_post's fallback path escapes HTML in title."""
    # Use fallback by not setting GROQ_API_KEY
    with mock.patch.dict(os.environ, {}, clear=False):
        # Ensure GROQ_API_KEY is not set
        if "GROQ_API_KEY" in os.environ:
            del os.environ["GROQ_API_KEY"]

        article = {
            "title": "Rust & C++ <performance> comparison",
            "source": "TechBlog",
            "url": "https://example.com/article",
            "summary": "Some content"
        }
        result = generate_post(article)
        # Should contain escaped versions in fallback
        assert "&amp;" in result
        assert "&lt;" in result and "&gt;" in result
        # Should NOT contain raw < or > in title
        assert "<b>Rust & C++ <performance>" not in result


def test_generate_post_fallback_escapes_html_in_source():
    """Verify that generate_post's fallback path escapes HTML in source."""
    with mock.patch.dict(os.environ, {}, clear=False):
        if "GROQ_API_KEY" in os.environ:
            del os.environ["GROQ_API_KEY"]

        article = {
            "title": "News",
            "source": "News & Events <Stream>",
            "url": "https://example.com/article",
            "summary": "Some content"
        }
        result = generate_post(article)
        # Should contain escaped versions
        assert "&amp;" in result
        assert "&lt;" in result and "&gt;" in result
        # Should NOT contain raw characters in source
        assert "News & Events <Stream></i>" not in result


from notifier import _is_hot


def test_is_hot_regular_threshold_score_3():
    assert _is_hot({"score": 3}) is True


def test_is_hot_below_threshold_without_buzz():
    assert _is_hot({"score": 2}) is False


def test_is_hot_buzz_rescues_score_2():
    """Новость, о которой пишут 2+ источника, отправляется даже при score 2."""
    assert _is_hot({"score": 2, "buzz": 2}) is True
    assert _is_hot({"score": 2, "buzz": 3}) is True


def test_is_hot_buzz_too_weak_for_score_1():
    assert _is_hot({"score": 1, "buzz": 3}) is False


def test_is_hot_defaults_missing_fields():
    assert _is_hot({}) is False
