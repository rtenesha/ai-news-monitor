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


from news_pipeline import extract_full_text


def test_extract_full_text_returns_none_on_empty_url():
    """Verify that extract_full_text handles empty/malformed URLs gracefully and
    returns None instead of raising ValueError from urllib.request.Request().
    This directly covers the fix to move Request() construction inside the try block."""
    result = extract_full_text("")
    assert result is None


def test_extract_full_text_returns_none_on_malformed_url():
    """Verify that extract_full_text handles scheme-less URLs gracefully and
    returns None instead of raising ValueError."""
    result = extract_full_text("not-a-valid-url")
    assert result is None


from news_pipeline import _parse_duplicate_groups, _apply_duplicate_groups


def test_parse_duplicate_groups_valid():
    raw = '{"duplicates": [[0, 2]]}'
    assert _parse_duplicate_groups(raw, n_items=3) == [[0, 2]]


def test_parse_duplicate_groups_ignores_out_of_range_indices():
    raw = '{"duplicates": [[0, 5]]}'
    assert _parse_duplicate_groups(raw, n_items=3) == []


def test_parse_duplicate_groups_ignores_single_item_groups():
    raw = '{"duplicates": [[0]]}'
    assert _parse_duplicate_groups(raw, n_items=3) == []


def test_parse_duplicate_groups_invalid_json_returns_empty():
    assert _parse_duplicate_groups("не могу разобрать", n_items=3) == []


def test_parse_duplicate_groups_none_input_returns_empty():
    """Verify that _parse_duplicate_groups handles None gracefully when Groq
    returns content=None, matching _parse_analysis_json's guard pattern."""
    assert _parse_duplicate_groups(None, n_items=3) == []


def test_apply_duplicate_groups_keeps_highest_score():
    articles = [
        {"title": "A", "score": 3},
        {"title": "B", "score": 5},
        {"title": "C", "score": 1},
    ]
    result = _apply_duplicate_groups(articles, [[0, 1]])
    assert result[0] == {"title": "B", "score": 5, "buzz": 2}
    assert result[1] == {"title": "C", "score": 1}


from news_pipeline import _hn_story_to_article, _github_trending_period, _google_news_time_operator, fetch_hackernews, fetch_github_trending


def test_hn_story_to_article_maps_fields():
    story = {"id": 123, "title": "Show HN: cool AI tool", "score": 250, "text": "details here"}
    article = _hn_story_to_article(story)
    assert article["title"] == "Show HN: cool AI tool"
    assert article["url"] == "https://news.ycombinator.com/item?id=123"
    assert article["source"] == "Hacker News"


def test_hn_story_to_article_prefers_external_url():
    story = {"id": 123, "title": "External link story", "score": 250, "url": "https://example.com/post"}
    article = _hn_story_to_article(story)
    assert article["url"] == "https://example.com/post"


def test_hn_story_to_article_skips_missing_title():
    assert _hn_story_to_article({"id": 1, "score": 200}) is None


def test_github_trending_period_short_window():
    assert _github_trending_period(24) == "past_24_hours"


def test_github_trending_period_long_window():
    assert _github_trending_period(168) == "past_28_days"


def test_google_news_time_operator_short_window():
    assert _google_news_time_operator(24) == "when:24h"


def test_google_news_time_operator_long_window():
    op = _google_news_time_operator(168)
    assert op.startswith("after:")


def test_hn_story_to_article_raises_on_missing_id():
    """Verify that _hn_story_to_article raises KeyError when 'id' field is missing.
    This tests that the per-item guard in fetch_hackernews is necessary."""
    story = {"title": "Story without ID", "score": 200}
    try:
        _hn_story_to_article(story)
        assert False, "Expected KeyError for missing 'id' field"
    except KeyError:
        pass  # Expected


def test_fetch_hackernews_skips_malformed_stories():
    """Verify that fetch_hackernews continues collecting articles even when
    one story object is missing the 'id' field (finding #1)."""
    from unittest.mock import patch
    import json

    # Mock API responses: first request returns top story IDs (1, 2, 3),
    # then individual story fetches: story 1 has no 'id', stories 2 and 3 are valid.
    responses = [
        json.dumps([1, 2, 3]).encode(),  # topstories response
        json.dumps({"title": "Story 1", "score": 200}).encode(),  # story 1: missing 'id'
        json.dumps({"id": 2, "title": "Valid story 2", "score": 150}).encode(),  # story 2: valid
        json.dumps({"id": 3, "title": "Valid story 3", "score": 200}).encode(),  # story 3: valid
    ]
    response_iter = iter(responses)

    def mock_urlopen(url, timeout=10):
        from io import BytesIO
        from unittest.mock import MagicMock
        response = MagicMock()
        response.__enter__ = lambda self: self
        response.__exit__ = lambda self, *args: None
        response.read = lambda: next(response_iter)
        return response

    with patch("urllib.request.urlopen", side_effect=mock_urlopen):
        articles = fetch_hackernews(hours=24, min_score=100)
    # Should have 2 articles (stories 2 and 3), not 0 or crash
    assert len(articles) == 2
    assert articles[0]["title"] == "Valid story 2"
    assert articles[1]["title"] == "Valid story 3"


def test_fetch_hackernews_skips_invalid_timestamp():
    """Verify that fetch_hackernews continues when a story has a non-numeric timestamp."""
    from unittest.mock import patch
    import json
    from datetime import datetime, timezone, timedelta

    # Use a recent timestamp to ensure it passes the cutoff filter
    recent_timestamp = int(datetime.now(timezone.utc).timestamp())

    responses = [
        json.dumps([1, 2]).encode(),  # topstories
        json.dumps({"id": 1, "title": "Story 1", "score": 200, "time": "not-a-number"}).encode(),  # invalid time
        json.dumps({"id": 2, "title": "Story 2", "score": 200, "time": recent_timestamp}).encode(),  # valid
    ]
    response_iter = iter(responses)

    def mock_urlopen(url, timeout=10):
        from unittest.mock import MagicMock
        response = MagicMock()
        response.__enter__ = lambda self: self
        response.__exit__ = lambda self, *args: None
        response.read = lambda: next(response_iter)
        return response

    with patch("urllib.request.urlopen", side_effect=mock_urlopen):
        articles = fetch_hackernews(hours=24, min_score=100)
    # Should have at least 1 article (story 2), not 0 or crash
    assert len(articles) >= 1
    assert any(a["title"] == "Story 2" for a in articles)


def test_fetch_github_trending_handles_non_dict_payload():
    """Verify that fetch_github_trending skips a language when the API returns
    non-dict JSON (e.g., null, list), instead of crashing (finding #3)."""
    from unittest.mock import patch
    import json

    responses = [
        b"null",  # Python: returns list - non-dict JSON
        json.dumps({"data": {"rows": [{"repo_name": "python/cpython", "stars": 50, "description": "Python"}]}}).encode(),  # TypeScript: valid
    ]
    response_iter = iter(responses)

    def mock_urlopen(url, timeout=15):
        from unittest.mock import MagicMock
        response = MagicMock()
        response.__enter__ = lambda self: self
        response.__exit__ = lambda self, *args: None
        response.read = lambda: next(response_iter)
        return response

    with patch("urllib.request.urlopen", side_effect=mock_urlopen):
        articles = fetch_github_trending(hours=24, languages=["Python", "TypeScript"], min_stars=5)
    # Should have 1 article from TypeScript language, not crash from null payload
    assert len(articles) == 1
    assert articles[0]["title"] == "python/cpython (+50⭐)"


def test_fetch_github_trending_skips_malformed_rows():
    """Verify that fetch_github_trending skips a malformed row (non-dict) and
    continues with other rows (finding #4)."""
    from unittest.mock import patch
    import json

    # Mock response: rows list contains a non-dict value and then valid rows
    responses = [
        json.dumps({
            "data": {
                "rows": [
                    "not-a-dict",  # malformed row
                    {"repo_name": "repo/one", "stars": 10, "description": "First"},
                    {"repo_name": "repo/two", "stars": 20, "description": "Second"},
                ]
            }
        }).encode(),
    ]
    response_iter = iter(responses)

    def mock_urlopen(url, timeout=15):
        from unittest.mock import MagicMock
        response = MagicMock()
        response.__enter__ = lambda self: self
        response.__exit__ = lambda self, *args: None
        response.read = lambda: next(response_iter)
        return response

    with patch("urllib.request.urlopen", side_effect=mock_urlopen):
        articles = fetch_github_trending(hours=24, languages=["Python"], min_stars=5)
    # Should have 2 articles (skipping the non-dict row), not 0 or crash
    assert len(articles) == 2
    assert articles[0]["title"] == "repo/one (+10⭐)"
    assert articles[1]["title"] == "repo/two (+20⭐)"


from news_pipeline import _strip_html, _parse_telegram_messages, _telegram_post_to_article

_TG_FIXTURE_TEMPLATE = """
<html><body>
<div class="tgme_widget_message_wrap js-widget_message_wrap">
  <div class="tgme_widget_message_text js-message_text" dir="auto">
    <b>Модели OpenAI оставляли себе записки</b><br/><br/>Во время обучения GPT-5.6
    исследователи обнаружили &quot;странные указания&quot; в пересказах.
  </div>
  <a class="tgme_widget_message_date" href="https://t.me/aioftheday/5208"><time datetime="{ts1}"></time></a>
</div>
<div class="tgme_widget_message_wrap js-widget_message_wrap">
  <div class="tgme_widget_message_text js-message_text" dir="auto">Короткий пост без разметки</div>
  <a class="tgme_widget_message_date" href="https://t.me/aioftheday/5207"><time datetime="{ts2}"></time></a>
</div>
<div class="tgme_widget_message_wrap js-widget_message_wrap">
  <div class="tgme_widget_message_photo"><img/></div>
  <a class="tgme_widget_message_date" href="https://t.me/aioftheday/5206"><time datetime="{ts3}"></time></a>
</div>
<div class="tgme_widget_message_wrap js-widget_message_wrap">
  <div class="tgme_widget_message_text js-message_text" dir="auto">Пост без времени</div>
  <a class="tgme_widget_message_date" href="https://t.me/aioftheday/5205"></a>
</div>
</body></html>
"""


def _tg_fixture():
    """Fixture with timestamps relative to now (2h/3h/4h ago), so the tests
    don't rot as the wall clock moves."""
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    return _TG_FIXTURE_TEMPLATE.format(
        ts1=(now - timedelta(hours=2)).isoformat(),
        ts2=(now - timedelta(hours=3)).isoformat(),
        ts3=(now - timedelta(hours=4)).isoformat(),
    )


def test_strip_html_removes_tags_and_unescapes_entities():
    raw = '<b>Заголовок</b><br/><br/>Текст с &quot;кавычками&quot; и &amp; амперсандом'
    text = _strip_html(raw)
    assert "<" not in text and ">" not in text
    assert '"кавычками"' in text
    assert "&amp;" not in text and "&" in text


def test_parse_telegram_messages_extracts_text_url_and_time():
    messages = _parse_telegram_messages(_tg_fixture(), "aioftheday")
    assert len(messages) == 2  # media-only and time-less blocks skipped
    first = messages[0]
    assert first["url"] == "https://t.me/aioftheday/5208"
    assert first["published"].tzinfo is not None
    assert "Модели OpenAI оставляли себе записки" in first["text"]
    assert '"странные указания"' in first["text"]


def test_parse_telegram_messages_no_html_returns_empty():
    assert _parse_telegram_messages("", "aioftheday") == []


def test_telegram_post_to_article_title_is_first_line():
    from datetime import datetime, timezone
    msg = {"text": "Модели OpenAI оставляли записки.\nВторая строка с деталями.",
           "url": "https://t.me/aioftheday/5208",
           "published": datetime(2026, 9, 18, 12, 33, tzinfo=timezone.utc)}
    article = _telegram_post_to_article(msg, "aioftheday")
    assert article["title"].startswith("Модели OpenAI оставляли записки.")
    assert article["url"] == "https://t.me/aioftheday/5208"
    assert article["source"] == "Telegram: @aioftheday"
    assert "Вторая строка" in article["summary"]


def test_telegram_post_to_article_skips_empty_text():
    msg = {"text": "", "url": "https://t.me/aioftheday/1", "published": None}
    assert _telegram_post_to_article(msg, "aioftheday") is None


from news_pipeline import fetch_telegram_channels, TELEGRAM_CHANNELS


def test_telegram_channels_list_has_19_channels():
    assert len(TELEGRAM_CHANNELS) == 19
    assert "vibecoding_tg" in TELEGRAM_CHANNELS
    assert "aioftheday" in TELEGRAM_CHANNELS
    assert "PushEnter" in TELEGRAM_CHANNELS


def test_fetch_telegram_channels_skips_failing_channel():
    from unittest.mock import patch
    from io import BytesIO

    def mock_urlopen(req, timeout=15):
        url = req.full_url if hasattr(req, "full_url") else req
        if "t.me/s/goodchannel" in url:
            resp = BytesIO(_tg_fixture().encode())
            resp.__enter__ = lambda self: self
            resp.__exit__ = lambda self, *args: None
            return resp
        raise RuntimeError("channel blocked")

    with patch("urllib.request.urlopen", side_effect=mock_urlopen):
        articles = fetch_telegram_channels(hours=24, channels=["goodchannel", "brokenchannel"])
    assert len(articles) == 2
    assert all(a["source"] == "Telegram: @goodchannel" for a in articles)


def test_fetch_telegram_channels_filters_old_posts():
    from unittest.mock import patch
    from io import BytesIO
    from datetime import datetime, timezone

    def mock_urlopen(req, timeout=15):
        resp = BytesIO(_tg_fixture().encode())
        resp.__enter__ = lambda self: self
        resp.__exit__ = lambda self, *args: None
        return resp

    with patch("urllib.request.urlopen", side_effect=mock_urlopen):
        articles = fetch_telegram_channels(hours=1, channels=["aioftheday"])
    assert articles == []  # fixture posts are from 2026-09-18, older than 1h


def test_apply_duplicate_groups_records_buzz():
    from news_pipeline import _apply_duplicate_groups
    articles = [
        {"title": "A", "score": 3},
        {"title": "B", "score": 5},
        {"title": "C", "score": 1},
    ]
    result = _apply_duplicate_groups(articles, [[0, 1]])
    assert result[0]["title"] == "B"
    assert result[0]["buzz"] == 2
    assert result[1].get("buzz", 1) == 1


from news_pipeline import _decode_google_news_url


def test_decode_google_news_old_format_embeds_url():
    """Old-format Google News IDs contain the publisher URL as base64."""
    import base64
    url = "https://example.com/real-article"
    enc = base64.urlsafe_b64encode(b'\x08\x13"\xd8\x01' + url.encode()).decode().rstrip("=")
    assert _decode_google_news_url(f"https://news.google.com/rss/articles/{enc}?oc=5") == url


def test_decode_google_news_passthrough_normal_urls():
    assert _decode_google_news_url("https://example.com/post") == "https://example.com/post"


def test_decode_google_news_new_format_returns_none_on_api_failure():
    """New opaque IDs need Google's internal API; if it fails, return None —
    callers must drop the article (source unverifiable)."""
    from unittest.mock import patch
    with patch("urllib.request.urlopen", side_effect=RuntimeError("blocked")):
        assert _decode_google_news_url(
            "https://news.google.com/rss/articles/CBMi2AFAU_yqLMgL5?oc=5"
        ) is None
