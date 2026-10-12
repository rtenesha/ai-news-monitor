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


# --- select_topics ---

def test_select_topics_ai_mode_keeps_total_ge_6_and_sorts():
    import editorial
    arts = [_article(f"ChatGPT новость {i}", url=f"https://a.example/{i}")
            for i in range(3)]
    strong = '{"criteria": {"новизна": 2, "польза": 2, "удивление": 2, "актуальность": 2, "пересылка": 2, "соответствие": 2}, "total": 12, "rubric": "Новое в ИИ", "post_format": "Б", "one_liner": "сильная новость"}'
    weak = '{"criteria": {"новизна": 0, "польза": 1, "удивление": 0, "актуальность": 1, "пересылка": 0, "соответствие": 1}, "total": 3, "rubric": "Новое в ИИ", "post_format": "А", "one_liner": "слабая"}'
    contents = [strong, weak, strong]  # article[1] слабая
    client = Mock()
    responses = [Mock() for _ in contents]
    for r, c in zip(responses, contents):
        r.choices = [Mock()]
        r.choices[0].message.content = c
    client.chat.completions.create.side_effect = responses

    with patch.object(news_pipeline, "extract_full_text", return_value="полный текст"):
        topics, used_ai = editorial.select_topics(arts, client)

    assert used_ai is True
    assert len(topics) == 2  # слабая (total 3) отброшена
    assert all(t["ai"] for t in topics)
    assert topics[0]["total"] == 12
    assert topics[0]["article"]["_full_text"] == "полный текст"


def test_select_topics_fallback_without_client_marks_non_ai():
    import editorial
    arts = [_article(f"ChatGPT новость {i}", url=f"https://a.example/{i}")
            for i in range(10)]
    topics, used_ai = editorial.select_topics(arts, None)

    assert used_ai is False
    assert len(topics) == editorial.TOPIC_LIST_MAX == 8
    assert all(t["ai"] is False for t in topics)
    assert all(t["total"] >= editorial.FALLBACK_TOTAL for t in topics)
    totals = [t["total"] for t in topics]
    assert totals == sorted(totals, reverse=True)


def test_select_topics_scorer_failure_falls_back_per_article():
    import editorial
    arts = [_article("ChatGPT новость", url="https://a.example/1")]
    client = Mock()
    client.chat.completions.create.side_effect = RuntimeError("down")
    with patch.object(news_pipeline, "extract_full_text", return_value=None):
        topics, used_ai = editorial.select_topics(arts, client)

    assert used_ai is True  # клиент был, но конкретная статья ушла в фолбэк
    assert topics[0]["ai"] is False


# --- build_card ---

_CARD_RAW = '''```json
{"topic_title": "Claude ставит скилы сам",
 "rubric": "Инструменты и приложения",
 "summary": "Claude Code сам нашёл нужный скил и подключил. Работает без ручных папок.",
 "why_interesting": "Раньше скилы ставили руками — это экономит час.",
 "for_whom": "Всем, кто работает с Claude",
 "risks": "Может выбрать не тот скил",
 "headline_variants": ["Claude научился ставить себе скилы 🔍",
                       "Больше не надо подключать скилы руками",
                       "Скилы сами прыгают в Claude"],
 "post_format": "Б",
 "visual_idea": "Скриншот автоподключения скила",
 "draft": {"headline": "Claude научился ставить себе скилы 🔍",
           "body": "Claude Code сам нашёл нужный скил и подключил его.\\n\\nРаньше делали руками.",
           "cta": "Кто-то уже пробовал?"},
 "claims": ["Claude Code сам находит скил", "Раньше подключали руками"]
}
```'''


def _topic(full_text="Claude Code сам подключает скилы. Раньше было руками."):
    import editorial
    art = _article()
    art["_full_text"] = full_text
    return {"article": art, "criteria": {"новизна": 2}, "total": 11,
            "rubric": "Инструменты и приложения", "post_format": "Б",
            "one_liner": "суть", "ai": True}


def test_build_card_parses_full_json_card():
    import editorial
    card = editorial.build_card(_topic(), _fake_client(_CARD_RAW))

    assert card["topic_title"] == "Claude ставит скилы сам"
    assert card["rubric"] == "Инструменты и приложения"
    assert card["post_format"] == "Б"
    assert len(card["headline_variants"]) == 3
    assert card["draft_headline"] == "Claude научился ставить себе скилы 🔍"
    assert "скил" in card["draft_body"]
    assert card["claims"] == ["Claude Code сам находит скил", "Раньше подключали руками"]
    assert card["no_ai"] is False
    assert card["truncated"] is False


def test_build_card_fallback_without_client_or_bad_json():
    import editorial
    topic = _topic()
    stub = editorial.build_card(topic, None)
    assert stub["no_ai"] is True
    assert stub["topic_title"] == topic["article"]["title"]
    assert stub["draft_body"] == ""
    assert stub["first_source_url"] == topic["article"]["url"]

    bad = editorial.build_card(topic, _fake_client("не JSON"))
    assert bad["no_ai"] is True


# --- fact_check_card ---

def _partially_checked_raw():
    verdicts = ('{"verdicts": ['
                '{"n": 1, "verdict": "ok", "text": "Claude Code сам находит скил", "query": "Claude Code skills"}, '
                '{"n": 2, "verdict": "uncertain", "text": "Переход на автоскилы был раньше", "query": "Claude skills авто"}'
                '], "headline": "", "body": "Claude Code сам находит скил. Автоскилы пришли ранее.", "cta": ""}')
    return str(verdicts)


def test_fact_check_card_splits_verdicts_and_fixes_draft():
    import editorial
    card = editorial.build_card(_topic(), _fake_client(_CARD_RAW))
    article = _article()
    article["_full_text"] = "Claude Code сам подключает скилы."
    with patch.object(news_pipeline, "fetch_google_news", return_value=[]):
        checked = editorial.fact_check_card(card, article, _fake_client(_partially_checked_raw()))

    assert checked["confirmed"] == ["Claude Code сам находит скил"]
    assert len(checked["needs_check"]) == 1
    assert "Переход на автоскилы был раньше" in checked["needs_check"][0]
    assert "Автоскилы пришли ранее" in checked["draft_body"]
    assert "Раньше делали руками" not in checked["draft_body"]


def test_fact_check_card_needs_no_client():
    import editorial
    card = editorial.build_card(_topic(), _fake_client(_CARD_RAW))
    original = card["draft_body"]
    checked = editorial.fact_check_card(card, _article(), None)

    assert checked["confirmed"] == []
    assert checked["needs_check"] == card["claims"]
    assert checked["draft_body"] == original


def test_fact_check_card_garbage_keeps_claims_unchecked():
    import editorial
    card = editorial.build_card(_topic(), _fake_client(_CARD_RAW))
    original = card["draft_body"]
    checked = editorial.fact_check_card(card, _article(title="x"), _fake_client("не JSON"))

    assert checked["confirmed"] == []
    assert checked["needs_check"] == card["claims"]
    assert checked["draft_body"] == original


def test_fact_check_card_corroborates_and_collects_sources():
    import editorial
    card = editorial.build_card(_topic(), _fake_client(_CARD_RAW))
    article = _article()
    article["_full_text"] = "текст."
    others = [{"title": "Claude skills автоподключение - РБК",
               "url": "https://news.example/s1"},
              {"title": "Claude skills автоподключение - Хабр",
               "url": "https://news.example/s2"},
              {"title": "тот же url - РБК", "url": article["url"]}]
    with patch.object(news_pipeline, "fetch_google_news", return_value=others):
        checked = editorial.fact_check_card(card, article, _fake_client(_partially_checked_raw()))

    assert checked["needs_check"][0].startswith("✓ (подтверждено другими СМИ)")
    urls = [s["url"] for s in checked["sources"]]
    assert article["url"] not in urls  # свой источник не попадает в «Доп. источники»
    assert len(urls) == 2


# --- форматирование ---

def test_render_topic_list_numbers_and_marks_priority():
    import editorial
    topics = [_topic(), dict(_topic(), total=7)]
    text = editorial.render_topic_list(topics, 24, used_ai=True)

    assert "1." in text and "2." in text
    assert "/card 1" in text and "/card 2" in text
    assert "11/12" in text and "7/12" in text
    assert "приоритет" in text and "резерв" in text


def test_render_topic_list_warns_without_ai():
    import editorial
    text = editorial.render_topic_list([_topic()], 24, used_ai=False)
    assert "без AI" in text


def test_format_card_messages_escapes_html():
    import editorial
    card = editorial.build_card(_topic(), _fake_client(_CARD_RAW))
    card["summary"] = "скрипт <b>жирный</b> & хак"
    card["draft_body"] = "тело <script>alert(1)&</script>"
    msgs = editorial.format_card_messages(card)

    assert "<b>жирный</b>" not in msgs[0]
    assert "&lt;b&gt;жирный&lt;/b&gt;" in msgs[0]
    draft_msg = [m for m in msgs if "Черновик" in m][0]
    assert "&lt;script&gt;" in draft_msg
    assert all(len(m) <= 4096 for m in msgs)


def test_format_card_messages_keeps_draft_separate():
    import editorial
    card = editorial.build_card(_topic(), _fake_client(_CARD_RAW))
    msgs = editorial.format_card_messages(card)
    assert "draft_body" not in msgs[0]
    assert card["draft_body"] in msgs[-1]
    assert "Черновик" in msgs[-1]


def test_format_card_messages_splits_long_body():
    import editorial
    card = editorial.build_card(_topic(), _fake_client(_CARD_RAW))
    card["draft_body"] = "абзац один\n\n" * 700  # ~7000 знаков
    msgs = editorial.format_card_messages(card)
    assert all(len(m) <= 4096 for m in msgs)
    assert len(msgs) > 2


def test_format_card_handles_hf_no_date():
    import editorial
    card = editorial.build_card(
        dict(_topic(), article=_article(published=None)), _fake_client(_CARD_RAW))
    card["published"] = None
    msgs = editorial.format_card_messages(card)
    assert "трендинг на момент сбора" in msgs[0] or "без даты" in msgs[0]


# --- ботовые хелперы ---

def test_parse_int_arg():
    import editorial
    assert editorial.parse_int_arg(["3"]) == 3
    assert editorial.parse_int_arg([]) is None
    assert editorial.parse_int_arg(["abc"]) is None
    assert editorial.parse_int_arg(["0"]) is None  # 0 не валиден как номер


def test_resolve_topic_bounds():
    import editorial
    topics = [_topic()]
    assert editorial.resolve_topic(topics, 1) is topics[0]
    assert editorial.resolve_topic(topics, 0) is None
    assert editorial.resolve_topic(topics, 2) is None
    assert editorial.resolve_topic([], 1) is None


def test_get_or_build_card_reuses_builtin():
    import editorial
    topic = _topic()
    client = _fake_client(_CARD_RAW)
    first = editorial.get_or_build_card(topic, client)

    # повторный вызов возвращает сохранённую карточку, второй build не выполнялся
    second = editorial.get_or_build_card(topic, None)
    assert second is first
    assert first["published"] == topic["article"]["published"]
