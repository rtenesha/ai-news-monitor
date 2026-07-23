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


def test_format_brief_escapes_html_in_title():
    """Verify that HTML special characters in title are escaped to prevent Telegram HTML injection."""
    article = {
        "title": "Node.js <5.0> & security risks",
        "source": "TechNews",
        "url": "https://example.com/article"
    }
    result = _format_brief(article)
    # Should contain escaped versions, not raw characters
    assert "&lt;" in result and "5.0&gt;" in result
    assert "&amp;" in result
    # Should NOT contain raw < or > inside the bold tag with the title
    assert "<b>Node.js <5.0>" not in result  # raw < would be present without escaping


def test_format_brief_escapes_html_in_source():
    """Verify that HTML special characters in source are escaped."""
    article = {
        "title": "AI News",
        "source": "News & Events <Stream>",
        "url": "https://example.com/article"
    }
    result = _format_brief(article)
    # Should contain escaped versions
    assert "&amp;" in result
    assert "&lt;" in result and "&gt;" in result
    # Should NOT contain raw characters in the source tag
    assert "<i>News & Events <Stream></i>" not in result


def test_format_brief_escapes_html_in_verdict():
    """Verify that HTML special characters in verdict are escaped."""
    article = {
        "title": "Article",
        "verdict": "This story involves <breaking> news & updates",
        "source": "Source",
        "url": "https://example.com/article"
    }
    result = _format_brief(article)
    # Should contain escaped versions
    assert "&lt;breaking&gt;" in result
    assert "&amp;" in result
    # Should NOT contain raw < or >
    assert "involves <breaking>" not in result
