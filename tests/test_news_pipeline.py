from news_pipeline import normalize_url, dedup_cross_source


def test_normalize_url_strips_tracking_params():
    a = normalize_url("https://example.com/article?utm_source=tg&utm_campaign=x")
    b = normalize_url("https://example.com/article")
    assert a == b


def test_normalize_url_ignores_scheme_www_and_trailing_slash():
    a = normalize_url("http://www.example.com/article/")
    b = normalize_url("https://example.com/article")
    assert a == b


def test_normalize_url_keeps_real_query_params():
    a = normalize_url("https://example.com/search?q=claude")
    b = normalize_url("https://example.com/search?q=gpt")
    assert a != b


def test_dedup_cross_source_merges_same_normalized_url():
    articles = [
        {"title": "A", "url": "https://example.com/x?utm_source=tg", "summary": "short",
         "source": "ZDNet", "published": None},
        {"title": "A", "url": "https://www.example.com/x", "summary": "a much longer summary text",
         "source": "Forbes", "published": None},
    ]
    result = dedup_cross_source(articles)
    assert len(result) == 1
    assert result[0]["summary"] == "a much longer summary text"
    assert result[0]["source"] == "Forbes + ZDNet"


def test_dedup_cross_source_keeps_distinct_urls():
    articles = [
        {"title": "A", "url": "https://example.com/1", "summary": "s1", "source": "X", "published": None},
        {"title": "B", "url": "https://example.com/2", "summary": "s2", "source": "Y", "published": None},
    ]
    result = dedup_cross_source(articles)
    assert len(result) == 2


from news_pipeline import AnalysisResult, _parse_analysis_json


def test_parse_analysis_json_valid():
    raw = '{"score": 4, "reason": "важный релиз", "summary": "OpenAI выпустила новую модель."}'
    result = _parse_analysis_json(raw)
    assert result == AnalysisResult(score=4, reason="важный релиз", summary="OpenAI выпустила новую модель.")


def test_parse_analysis_json_handles_markdown_fence_and_prose():
    raw = 'Вот оценка:\n```json\n{"score": 2, "reason": "so-so", "summary": "Что-то произошло."}\n```'
    result = _parse_analysis_json(raw)
    assert result is not None
    assert result.score == 2


def test_parse_analysis_json_invalid_json_returns_none():
    assert _parse_analysis_json("не могу оценить эту статью") is None


def test_parse_analysis_json_out_of_range_score_returns_none():
    raw = '{"score": 9, "reason": "x", "summary": "y"}'
    assert _parse_analysis_json(raw) is None


def test_parse_analysis_json_missing_field_returns_none():
    raw = '{"score": 3, "summary": "y"}'
    assert _parse_analysis_json(raw) is None


def test_parse_analysis_json_none_input_returns_none():
    """Verify that score_article_ai never raises when API returns None content."""
    assert _parse_analysis_json(None) is None


from news_pipeline import _extract_from_html

_ARTICLE_HTML = """
<html><body>
<nav>Главная | Новости | Контакты</nav>
<article>
<p>""" + ("Это содержательный абзац статьи про новую модель ИИ, который повторяется. " * 6) + """</p>
</article>
<footer>© 2026 Test Site. Все права защищены.</footer>
</body></html>
"""

_THIN_HTML = "<html><body><p>Слишком коротко.</p></body></html>"


def test_extract_from_html_returns_article_body():
    text = _extract_from_html(_ARTICLE_HTML)
    assert text is not None
    assert "содержательный абзац" in text
    assert "Контакты" not in text
    assert "Все права защищены" not in text


def test_extract_from_html_returns_none_for_thin_content():
    assert _extract_from_html(_THIN_HTML) is None


def test_extract_from_html_returns_none_when_trafilatura_raises():
    """Verify that _extract_from_html catches trafilatura exceptions and
    returns None instead of propagating, honoring the 'never raise' contract."""
    from unittest.mock import patch
    with patch("trafilatura.extract") as mock_extract:
        mock_extract.side_effect = RuntimeError("malformed HTML causes parser crash")
        result = _extract_from_html("<html>garbage</html>")
        assert result is None
