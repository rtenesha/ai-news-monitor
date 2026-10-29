"""Tests for scout.py: candidate collection, 5-criteria scoring, list,
cards per the 2026-10-29 scout spec, card fact-checking, rendering."""

from unittest.mock import Mock, patch

import news_pipeline


def _article(title="Claude научился ставить скилы сам", url="https://a.example/1",
             summary="Anthropic skills agent", **extra):
    a = {"title": title, "url": url, "summary": summary,
         "source": "Test", "published": None}
    a.update(extra)
    return a


def editorial_feeds():
    import editorial
    return editorial.EDITORIAL_FEEDS


# --- collect_scout_topics ---


def test_collect_scout_topics_merges_all_sources_dedups_and_caps():
    import scout
    all_feeds = scout.SCOUT_FEEDS + editorial_feeds()
    feeds = {f["name"]: ([_article(url="https://same.example/1",
                                   summary="ИИ нейросеть ChatGPT",
                                   source=f["name"])], True) for f in all_feeds}
    with patch.object(news_pipeline, "fetch_rss_feed",
                      side_effect=lambda name, url, hours: feeds.get(name, ([], True))), \
         patch.object(news_pipeline, "fetch_hackernews", return_value=[]), \
         patch.object(news_pipeline, "fetch_github_trending", return_value=[]), \
         patch.object(news_pipeline, "fetch_hf_trending", return_value=[]), \
         patch.object(news_pipeline, "fetch_reddit", return_value=[]), \
         patch.object(news_pipeline, "fetch_producthunt", return_value=[]), \
         patch.object(scout, "fetch_hf_papers", return_value=[]):
        topics = scout.collect_scout_topics(hours=24)

    # 10 фидов отдали одинаковый URL → дедуп склеил в 1
    assert len(topics) == 1
    assert " + " in topics[0]["source"]


def test_collect_scout_topics_drops_non_scout_articles():
    import scout
    borsch = _article(title="рецепт борща", url="https://a.example/borsch",
                      summary="готовим вкусно со свёклой")
    robot = _article(title="Robot learned to fold laundry",
                     url="https://a.example/robot", summary="demo video")
    with patch.object(news_pipeline, "fetch_rss_feed",
                      return_value=([borsch, robot], True)), \
         patch.object(news_pipeline, "fetch_hackernews", return_value=[]), \
         patch.object(news_pipeline, "fetch_github_trending", return_value=[]), \
         patch.object(news_pipeline, "fetch_hf_trending", return_value=[]), \
         patch.object(news_pipeline, "fetch_reddit", return_value=[]), \
         patch.object(news_pipeline, "fetch_producthunt", return_value=[]), \
         patch.object(scout, "fetch_hf_papers", return_value=[]):
        topics = scout.collect_scout_topics(hours=24)

    urls = {t["url"] for t in topics}
    assert "https://a.example/borsch" not in urls
    assert "https://a.example/robot" in urls  # рубричный фильтр шире KEYWORDS


def test_collect_scout_topics_caps_at_20_and_orders_by_score():
    import scout
    many = [_article(f"AI news item number {i} ChatGPT", url=f"https://a.example/{i}")
            for i in range(25)]
    strong = _article("Humanoid robot demo of a new skill", url="https://a.example/strong",
                      summary="robot demo video")
    with patch.object(news_pipeline, "fetch_rss_feed",
                      return_value=(many + [strong], True)), \
         patch.object(news_pipeline, "fetch_hackernews", return_value=[]), \
         patch.object(news_pipeline, "fetch_github_trending", return_value=[]), \
         patch.object(news_pipeline, "fetch_hf_trending", return_value=[]), \
         patch.object(news_pipeline, "fetch_reddit", return_value=[]), \
         patch.object(news_pipeline, "fetch_producthunt", return_value=[]), \
         patch.object(scout, "fetch_hf_papers", return_value=[]):
        topics = scout.collect_scout_topics(hours=24)

    assert len(topics) == scout.CANDIDATE_CAP == 20
    assert topics[0]["url"] == "https://a.example/strong"


def test_fetch_hf_papers_parses_and_never_raises():
    import scout
    fixture = """
    <div><a href="/papers/2510.01010">Learning Rubik's Cube with a Quadruped Robot</a></div>
    <div><a href="/papers/2510.9999">Something Else About Robots</a></div>
    """
    resp = Mock()
    resp.__enter__ = Mock(return_value=resp)
    resp.__exit__ = Mock(return_value=False)
    resp.read = Mock(return_value=fixture.encode())
    resp.status = 200
    with patch.object(news_pipeline.urllib.request, "urlopen", return_value=resp):
        papers = scout.fetch_hf_papers()

    assert len(papers) == 2
    assert papers[0]["url"] == "https://huggingface.co/papers/2510.01010"
    assert "Rubik" in papers[0]["title"]
    assert papers[0]["source"] == "HF Papers"
    assert papers[0]["published"] is None

    with patch.object(news_pipeline.urllib.request, "urlopen",
                      side_effect=RuntimeError("down")):
        assert scout.fetch_hf_papers() == []

# --- score_scout_topic_ai ---


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


_SCORE_RAW = ('{"criteria": {"неожиданность": 2, "наглядность": 2, "понятность": 1, '
              '"релевантность": 2, "потенциал обсуждения": 1}, "total": 9, '
              '"category": "А", "one_liner": "Робот сам собрал кубик"}')


def test_score_parses_valid_json():
    import scout
    result = scout.score_scout_topic_ai(_article(), _fake_client(_SCORE_RAW))
    assert result is not None
    assert result["total"] == 8
    assert result["criteria"]["неожиданность"] == 2
    assert result["category"] == "А"
    assert result["one_liner"] == "Робот сам собрал кубик"


def test_score_recomputes_total():
    import scout
    raw = _SCORE_RAW.replace('"total": 9', '"total": 55')
    assert scout.score_scout_topic_ai(_article(), _fake_client(raw))["total"] == 8


def test_score_rejects_out_of_range():
    import scout
    raw = _SCORE_RAW.replace('"неожиданность": 2', '"неожиданность": 3')
    assert scout.score_scout_topic_ai(_article(), _fake_client(raw)) is None


def test_score_rejects_unknown_category():
    import scout
    assert scout.score_scout_topic_ai(
        _article(), _fake_client(_SCORE_RAW.replace('"category": "А"', '"category": "Ж"'))) is None


def test_score_drops_pustishka():
    import scout
    raw = _SCORE_RAW.replace('"category": "А"', '"category": "пустышка"')
    assert scout.score_scout_topic_ai(_article(), _fake_client(raw)) is None


def test_score_none_on_garbage_client_none():
    import scout
    assert scout.score_scout_topic_ai(_article(), _fake_client("не JSON вообще")) is None
    client = Mock()
    client.chat.completions.create.side_effect = RuntimeError("down")
    assert scout.score_scout_topic_ai(_article(), client) is None
    assert scout.score_scout_topic_ai(_article(), None) is None


# --- select_scout_topics ---


_STRONG = '{"criteria": {"неожиданность": 2, "наглядность": 2, "понятность": 2, "релевантность": 2, "потенциал обсуждения": 2}, "total": 10, "category": "А", "one_liner": "сильная"}'
_WEAK = '{"criteria": {"неожиданность": 1, "наглядность": 0, "понятность": 1, "релевантность": 0, "потенциал обсуждения": 1}, "total": 3, "category": "Б", "one_liner": "слабая"}'


def test_select_scout_keeps_ge_6_and_sorts():
    import scout
    arts = [_article(f"ChatGPT новость {i}", url=f"https://a.example/{i}") for i in range(3)]
    client = Mock()
    responses = [Mock() for _ in [_STRONG, _WEAK, _STRONG]]
    for r, c in zip(responses, [_STRONG, _WEAK, _STRONG]):
        r.choices = [Mock()]
        r.choices[0].message.content = c
    client.chat.completions.create.side_effect = responses
    with patch.object(news_pipeline, "extract_full_text", return_value="полный текст"):
        topics, used_ai = scout.select_scout_topics(arts, client)

    assert used_ai is True
    assert len(topics) == 2  # слабая (total 3 < 6) отброшена
    assert all(t["ai"] for t in topics)
    assert topics[0]["total"] == 10
    assert topics[0]["article"]["_full_text"] == "полный текст"


def test_select_scout_fallback_without_client():
    import scout
    arts = [_article(f"ChatGPT новость {i}", url=f"https://a.example/{i}") for i in range(12)]
    topics, used_ai = scout.select_scout_topics(arts, None)

    assert used_ai is False
    assert len(topics) == scout.SCOUT_LIST_MAX == 10
    assert all(t["ai"] is False for t in topics)
    totals = [t["total"] for t in topics]
    assert totals == sorted(totals, reverse=True)


def test_select_scout_per_article_fallback():
    import scout
    arts = [_article("ChatGPT новость", url="https://a.example/1")]
    client = Mock()
    client.chat.completions.create.side_effect = RuntimeError("down")
    with patch.object(news_pipeline, "extract_full_text", return_value=None):
        topics, used_ai = scout.select_scout_topics(arts, client)

    assert used_ai is True  # клиент был, но конкретная статья ушла в фолбэк
    assert topics[0]["ai"] is False


# --- render_scout_list ---


def _scout_topic(total=9, published=None, url="https://a.example/1",
                 source="arXiv cs.RO", one_liner="Робот сам собрал кубик"):
    import scout
    return {"article": {"title": "Robot solves cube", "url": url, "source": source,
                        "published": published, "summary": ""},
            "criteria": {}, "total": total, "category": "А",
            "one_liner": one_liner, "ai": True}


def test_render_scout_list_numbering_scores_and_links():
    import scout
    text = scout.render_scout_list([_scout_topic()], 48, used_ai=True)
    assert "1." in text
    assert "/scout 1" in text
    assert "9/10" in text
    assert "приоритет" in text
    assert 'href="https://a.example/1"' in text


def test_render_scout_list_human_category_names():
    import scout
    text = scout.render_scout_list([_scout_topic()], 48, used_ai=True)
    assert "Роботы и эксперименты" in text


def test_render_scout_list_reserves_sub_priority():
    import scout
    text = scout.render_scout_list([_scout_topic(total=6)], 48, used_ai=True)
    assert "резерв" in text


def test_render_scout_list_escapes_malicious_url():
    import scout
    topic = _scout_topic(url='https://a.example/1"><a href="https://phish.example/x">')
    text = scout.render_scout_list([topic], 48, used_ai=True)
    assert 'href="https://phish.example' not in text
    assert "&quot;&gt;&lt;a" in text


def test_render_scout_list_dateless_badge():
    import scout
    text = scout.render_scout_list([_scout_topic(published=None)], 48, used_ai=True)
    assert "без даты" in text
    from datetime import datetime
    dated = _scout_topic(published=datetime(2026, 10, 28))
    text = scout.render_scout_list([dated], 48, used_ai=True)
    assert "без даты" not in text


def test_render_scout_list_empty_and_no_ai():
    import scout
    text = scout.render_scout_list([], 48, used_ai=True)
    assert "Ничего необычного" in text and "/scout 168" in text
    text = scout.render_scout_list([_scout_topic()], 48, used_ai=False)
    assert "без AI" in text
