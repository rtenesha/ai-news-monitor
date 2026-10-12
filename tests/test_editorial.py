"""Tests for editorial.py: topic collection, 6-criteria AI scoring,
editor cards per the 2026-10-12 spec, card fact-checking, formatting."""

from unittest.mock import Mock, patch

import news_pipeline


def _fake_client(content: str):
    """Minimal Groq client stand-in: chat.completions.create -> given content."""
    class _Message:
        def __init__(self, c): self.content = c

    class _Choice:
        def __init__(self, c): self.message = _Message(c)

    class _Resp:
        def __init__(self, c): self.choices = [_Choice(c)]

    class _Completions:
        @staticmethod
        def create(**kwargs):
            return _Resp(content)

    class _Chat:
        completions = _Completions()

    class _Client:
        chat = _Chat()

    return _Client()


def _article(title="Claude научился ставить скилы сам", url="https://a.example/1",
             summary="Anthropic skills agent", **extra):
    a = {"title": title, "url": url, "summary": summary,
         "source": "Test", "published": None}
    a.update(extra)
    return a


# --- collect_topics ---

def test_collect_topics_merges_sources_dedups_and_caps():
    import editorial
    feeds = {f["name"]: ([_article(summary="ИИ нейросеть ChatGPT", source=f["name"])], True)
             for f in editorial.EDITORIAL_FEEDS}
    with patch.object(news_pipeline, "fetch_rss_feed",
                      side_effect=lambda name, url, hours: feeds.get(name, ([], True))), \
         patch.object(news_pipeline, "fetch_hackernews", return_value=[]), \
         patch.object(news_pipeline, "fetch_github_trending", return_value=[]), \
         patch.object(news_pipeline, "fetch_hf_trending", return_value=[]):
        topics = editorial.collect_topics(hours=24)

    # 6 фидов отдали одинаковый URL → дедуп склеил в 1
    assert len(topics) == 1
    assert " + " in topics[0]["source"]


def test_collect_topics_filters_out_non_ai_and_caps_at_15():
    import editorial
    many = [_article(f"title {i} ChatGPT agent", url=f"https://a.example/{i}")
            for i in range(20)]
    no_ai = _article(title="рецепт борща", url="https://a.example/borsch",
                     summary="готовим вкусно")

    with patch.object(news_pipeline, "fetch_rss_feed",
                      return_value=(many + [no_ai], True)), \
         patch.object(news_pipeline, "fetch_hackernews", return_value=[]), \
         patch.object(news_pipeline, "fetch_github_trending", return_value=[]), \
         patch.object(news_pipeline, "fetch_hf_trending", return_value=[]):
        topics = editorial.collect_topics(hours=24)

    assert len(topics) == editorial.CANDIDATE_CAP == 15
    urls = {t["url"] for t in topics}
    assert "https://a.example/borsch" not in urls


def test_collect_topics_orders_by_keyword_score_desc():
    import editorial
    weak = [_article("слабая ИИ заметка", url="https://a.example/w1",
                     summary="просто наблюдение")]
    strong = [_article("ChatGPT Claude GPT промты", url="https://a.example/s2",
                       summary="agent skills")]
    with patch.object(news_pipeline, "fetch_rss_feed",
                      return_value=(weak + strong, True)), \
         patch.object(news_pipeline, "fetch_hackernews", return_value=[]), \
         patch.object(news_pipeline, "fetch_github_trending", return_value=[]), \
         patch.object(news_pipeline, "fetch_hf_trending", return_value=[]):
        topics = editorial.collect_topics(hours=24)

    assert topics[0]["url"] == "https://a.example/s2"

# --- score_topic_ai ---

def _score_raw(total_note="10"):
    return ('{"criteria": {"новизна": 2, "польза": 2, "удивление": 1, '
            '"актуальность": 2, "пересылка": 1, "соответствие": 2}, '
            '"total": ' + total_note + ', "rubric": "Инструменты и приложения", '
            '"post_format": "Б", "one_liner": "Скилы ставятся сами"}')


def test_score_topic_ai_parses_valid_json():
    import editorial
    result = editorial.score_topic_ai(_article(), _fake_client(_score_raw()))
    assert result is not None
    assert result["total"] == 10
    assert result["criteria"]["новизна"] == 2
    assert result["rubric"] == "Инструменты и приложения"
    assert result["post_format"] == "Б"
    assert result["one_liner"] == "Скилы ставятся сами"


def test_score_topic_ai_recomputes_total_from_criteria():
    # модель соврала про total=12, сумма критериев 10 — код пересчитывает
    import editorial
    result = editorial.score_topic_ai(_article(), _fake_client(_score_raw("12")))
    assert result["total"] == 10


def test_score_topic_ai_rejects_out_of_range_criteria():
    import editorial
    raw = ('{"criteria": {"новизна": 3, "польза": 2, "удивление": 1, '
           '"актуальность": 2, "пересылка": 2, "соответствие": 0}, '
           '"total": 10, "rubric": "Новое в ИИ", "post_format": "А", "one_liner": "x"}')
    assert editorial.score_topic_ai(_article(), _fake_client(raw)) is None


def test_score_topic_ai_rejects_unknown_rubric_and_format():
    import editorial
    bad_rubric = _score_raw().replace("Инструменты и приложения", "Финансы")
    assert editorial.score_topic_ai(_article(), _fake_client(bad_rubric)) is None
    bad_fmt = _score_raw().replace('"Б"', '"Ж"')
    assert editorial.score_topic_ai(_article(), _fake_client(bad_fmt)) is None


def test_score_topic_ai_drops_reject_rubric():
    import editorial
    raw = _score_raw().replace("Инструменты и приложения", "Инфоповод-пустышка")
    assert editorial.score_topic_ai(_article(), _fake_client(raw)) is None


def test_score_topic_ai_none_on_garbage_or_exception():
    import editorial
    assert editorial.score_topic_ai(_article(), _fake_client("не JSON вообще")) is None
    client = Mock()
    client.chat.completions.create.side_effect = RuntimeError("down")
    assert editorial.score_topic_ai(_article(), client) is None
    assert editorial.score_topic_ai(_article(), None) is None
