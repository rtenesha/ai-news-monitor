from bot import _format_brief


def test_format_brief_uses_verdict_when_present():
    article = {"title": "Заголовок статьи", "verdict": "Короткое описание сути новости.",
               "source": "ZDNet", "url": "https://example.com/a"}
    result = _format_brief(article)
    assert "Заголовок статьи" in result
    assert "Короткое описание сути новости." in result
    assert "https://example.com/a" in result


def test_format_brief_falls_back_to_title_without_verdict():
    article = {"title": "Заголовок статьи", "source": "ZDNet", "url": "https://example.com/a"}
    result = _format_brief(article)
    assert result.count("Заголовок статьи") == 2  # bold title + fallback "verdict" line
