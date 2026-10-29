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


def test_collect_scout_topics_widens_arxiv_window():
    """arXiv публикует с лагом: даже свежие статьи старше 48ч, поэтому
    его фид должен запрашиваться с окном не уже 96ч независимо от hours."""
    import scout
    calls = []

    def fake_rss(name, url, hours):
        calls.append((name, hours))
        return ([], True)

    with patch.object(news_pipeline, "fetch_rss_feed", side_effect=fake_rss), \
         patch.object(news_pipeline, "fetch_hackernews", return_value=[]), \
         patch.object(news_pipeline, "fetch_github_trending", return_value=[]), \
         patch.object(news_pipeline, "fetch_hf_trending", return_value=[]), \
         patch.object(news_pipeline, "fetch_reddit", return_value=[]), \
         patch.object(news_pipeline, "fetch_producthunt", return_value=[]), \
         patch.object(scout, "fetch_hf_papers", return_value=[]):
        scout.collect_scout_topics(hours=24)

    arxiv = dict(calls).get("arXiv cs.RO")
    assert arxiv is not None and arxiv >= 96


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


# --- build_scout_card ---


_CARD_RAW = '''```json
{"hook": "Робот сам собрал кубик Рубика ⚡",
 "what_happened": "Команда X показала четвероногого робота, который сам собрал кубик. Демо снято на видео 28.10.",
 "why_interesting": "Такое раньше делали только промышленные манипуляторы.",
 "post_format": "необычный эксперимент", "visual": "видео демо в статье",
 "score_why": "высокая неожиданность и наглядность",
 "claims": ["Robot собрал кубик Рубика", "Видео от 28.10"]}
```'''


def _card_topic(full_text="Robot собрал кубик Рубика. Демо на видео."):
    import scout
    art = {"title": "Robot solves cube", "url": "https://a.example/1",
           "source": "arXiv cs.RO", "published": None, "summary": "суть",
           "_full_text": full_text}
    return {"article": art, "criteria": {"неожиданность": 2}, "total": 9,
            "category": "А", "one_liner": "суть", "ai": True}


def test_build_scout_card_parses_full_json():
    import scout
    card = scout.build_scout_card(_card_topic(), _fake_client(_CARD_RAW))
    assert card["hook"] == "Робот сам собрал кубик Рубика ⚡"
    assert "четвероногого робота" in card["what_happened"]
    assert card["post_format"] == "необычный эксперимент"
    assert len(card["claims"]) == 2
    assert card["no_ai"] is False
    assert card["first_source_url"] == "https://a.example/1"
    assert card["total"] == 9
    assert card["category"] == "А"


def test_build_scout_card_rejects_bad_format():
    import scout
    raw = _CARD_RAW.replace("необычный эксперимент", "статья-расследование")
    card = scout.build_scout_card(_card_topic(), _fake_client(raw))
    assert card["no_ai"] is True
    assert card["hook"] == "Robot solves cube"


def test_build_scout_card_stub_without_client():
    import scout
    card = scout.build_scout_card(_card_topic(), None)
    assert card["no_ai"] is True
    assert card["what_happened"] == "суть"
    assert card["status"] == "требует проверки"
    assert card["confirmed"] == [] and card["needs_check"] == []
    assert card["visual"] == "не найден"


def test_get_or_build_scout_card_caches():
    import scout
    topic = _card_topic()
    first = scout.get_or_build_scout_card(topic, None)
    second = scout.get_or_build_scout_card(topic, None)
    assert second is first
    assert first["published"] == topic["article"]["published"]


# --- fact_check_scout_card ---


_FC_RAW = ('{"verdicts": ['
           '{"n": 1, "verdict": "ok", "text": "Robot собрал кубик Рубика", "query": "robot rubiks cube"}, '
           '{"n": 2, "verdict": "uncertain", "text": "Видео от 28.10", "query": "robot rubiks video"}], '
           '"hook": "", "what_happened": "Команда X показала робота, собравшего кубик."}')

_FC_ALL_OK = ('{"verdicts": ['
              '{"n": 1, "verdict": "ok", "text": "Robot собрал кубик Рубика", "query": "robot rubiks cube"}, '
              '{"n": 2, "verdict": "ok", "text": "Видео от 28.10", "query": "robot rubiks video"}], '
              '"hook": "", "what_happened": ""}')


def _checked_card():
    import scout
    card = scout.build_scout_card(_card_topic(), _fake_client(_CARD_RAW))
    article = {"title": "Robot solves cube", "url": "https://a.example/1",
               "source": "arXiv cs.RO", "published": None,
               "_full_text": "Robot собрал кубик Рубика."}
    return card, article


def test_fact_check_scout_card_splits_verdicts_and_rewrites():
    import scout
    card, article = _checked_card()
    original_hook = card["hook"]
    with patch.object(news_pipeline, "fetch_google_news", return_value=[]):
        checked = scout.fact_check_scout_card(card, article, _fake_client(_FC_RAW))

    assert checked["confirmed"] == ["Robot собрал кубик Рубика"]
    assert len(checked["needs_check"]) == 1
    assert "Видео от 28.10" in checked["needs_check"][0]
    assert "собравшего кубик" in checked["what_happened"]
    assert checked["hook"] == original_hook  # пусто в ответе — не тронут


def test_fact_check_scout_card_without_client():
    import scout
    card, article = _checked_card()
    original_what = card["what_happened"]
    checked = scout.fact_check_scout_card(card, article, None)

    assert checked["confirmed"] == []
    assert checked["needs_check"] == card["claims"]
    assert checked["what_happened"] == original_what
    assert checked["status"] == "требует проверки"


def test_fact_check_scout_card_wrong_claim_dropped():
    import scout
    card, article = _checked_card()
    raw = '{"verdicts": [{"n": 1, "verdict": "wrong", "text": "", "query": ""}], ' \
          '"hook": "", "what_happened": "Подтверждённая версия из одного факта."}'
    checked = scout.fact_check_scout_card(card, article, _fake_client(raw))

    assert "cubик" not in str(checked["confirmed"])
    assert checked["confirmed"] == []
    assert checked["needs_check"] == []
    assert "Подтверждённая версия" in checked["what_happened"]


def test_fact_check_scout_card_corroboration_and_sources():
    import scout
    card, article = _checked_card()
    others = [{"title": "Robot собрал кубик - РБК", "url": "https://news.example/s1"},
              {"title": "Robot собрал кубик - Хабр", "url": "https://news.example/s2"},
              {"title": "тот же url - РБК", "url": article["url"]}]
    with patch.object(news_pipeline, "fetch_google_news", return_value=others):
        checked = scout.fact_check_scout_card(card, article, _fake_client(_FC_RAW))

    assert checked["needs_check"][0].startswith("✓ (подтверждено другими СМИ)")
    urls = [s["url"] for s in checked["sources"]]
    assert article["url"] not in urls  # свой источник не попадает в доп. источники
    assert len(urls) == 2


def test_verification_status_labels():
    import scout
    card, article = _checked_card()
    # все ok → подтверждено
    with patch.object(news_pipeline, "fetch_google_news", return_value=[]):
        checked = scout.fact_check_scout_card(dict(card), article,
                                              _fake_client(_FC_ALL_OK))
    assert checked["status"] == "подтверждено"

    # ok + uncertain с корроборацией → частично подтверждено
    others = [{"title": "Robot кубик - РБК", "url": "https://news.example/s1"},
              {"title": "Robot кубик - Хабр", "url": "https://news.example/s2"}]
    with patch.object(news_pipeline, "fetch_google_news", return_value=others):
        checked = scout.fact_check_scout_card(dict(card), article,
                                              _fake_client(_FC_RAW))
    assert checked["status"] == "частично подтверждено"

    # uncertain без корроборации → требует проверки
    with patch.object(news_pipeline, "fetch_google_news", return_value=[]):
        checked = scout.fact_check_scout_card(dict(card), article,
                                              _fake_client(_FC_RAW))
    assert checked["status"] == "требует проверки"


# --- render_scout_card_messages ---


def test_render_scout_card_all_fields_present():
    import scout
    card, article = _checked_card()
    with patch.object(news_pipeline, "fetch_google_news", return_value=[]):
        card = scout.fact_check_scout_card(card, article, _fake_client(_FC_RAW))
    msgs = scout.render_scout_card_messages({"category": "А"}, card)
    joined = "\n".join(msgs)
    assert "Робот сам собрал кубик Рубика ⚡" in joined  # hook
    assert card["what_happened"] == "Команда X показала робота, собравшего кубик."  # rewrite применён
    assert "Что произошло" in joined
    assert "необычный эксперимент" in joined
    assert "высокая неожиданность" in joined  # score_why
    assert all(len(m) <= 4096 for m in msgs)


def test_render_scout_card_escapes_html():
    import scout
    card, article = _checked_card()
    card["what_happened"] = "скрипт <b>жирный</b> & хак <script>alert(1)</script>"
    msgs = scout.render_scout_card_messages({"category": "А"}, card)
    joined = "\n".join(msgs)
    assert "<script>" not in joined
    assert "&lt;script&gt;" in joined
    assert "&lt;b&gt;жирный&lt;/b&gt;" in joined


def test_render_scout_card_status_and_sources():
    import scout
    card, article = _checked_card()
    card["status"] = "подтверждено"
    card["confirmed"] = ["Robot собрал кубик Рубика"]
    card["needs_check"] = []
    card["sources"] = [{"title": "РБК", "url": "https://news.example/s1"}]
    msgs = scout.render_scout_card_messages({"category": "А"}, card)
    joined = "\n".join(msgs)
    assert "подтверждено" in joined
    assert 'href="https://a.example/1"' in joined          # первоисточник
    assert 'href="https://news.example/s1"' in joined      # доп. источник


def test_render_scout_card_dateless():
    import scout
    card, article = _checked_card()
    card["published"] = None
    msgs = scout.render_scout_card_messages({"category": "А"}, card)
    assert "трендинг на момент сбора" in "\n".join(msgs)
