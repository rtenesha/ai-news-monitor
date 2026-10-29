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