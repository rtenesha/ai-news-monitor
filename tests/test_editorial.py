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