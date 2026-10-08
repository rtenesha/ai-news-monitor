"""Tests for the hourly digest in notifier.py: one post every 2h instead of
a post per hot article, with fact-checking (verdicts vs article text +
web corroboration via Google News) before sending."""

import notifier


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


def _hot():
    return [
        {"title": "OpenAI выпустила Sora 2", "url": "https://openai.com/sora",
         "summary": "OpenAI opened Sora 2 access for everyone",
         "source": "OpenAI Blog", "score": 4, "_full_text": "OpenAI текст про Sora 2."},
        {"title": "Claude Skills вышли в GA",
         "url": "https://anthropic.com/skills", "summary": "Anthropic skills",
         "source": "Anthropic", "score": 5, "_full_text": "Anthropic текст про skills."},
    ]


# --- _parse_digest_json ---

def test_parse_digest_json_valid():
    raw = ('{"items": [{"n": 1, "headline": "Sora открыли всем 💡", "text": "текст"}, '
           '{"n": 2, "headline": "Skills в GA 🚀", "text": "текст 2"}], "cta": "А вы пробовали?"}')
    parsed = notifier._parse_digest_json(raw)
    assert parsed is not None
    assert len(parsed["items"]) == 2
    assert parsed["items"][0]["n"] == 1
    assert parsed["cta"] == "А вы пробовали?"


def test_parse_digest_json_handles_markdown_fence():
    raw = '```json\n{"items": [{"n": 1, "headline": "H", "text": "T"}], "cta": "C"}\n```'
    parsed = notifier._parse_digest_json(raw)
    assert parsed is not None
    assert parsed["items"][0]["headline"] == "H"


def test_parse_digest_json_invalid_returns_none():
    assert notifier._parse_digest_json("не JSON вообще") is None


def test_parse_digest_json_missing_items_returns_none():
    assert notifier._parse_digest_json('{"cta": "только CTA"}') is None


# --- generate_digest ---

def test_generate_digest_fallback_without_client():
    hot = _hot()
    items, cta = notifier.generate_digest(hot, client=None)
    assert [i["headline"] for i in items] == [a["title"] for a in hot]
    assert [i["article_index"] for i in items] == [0, 1]
    assert cta == ""


def test_generate_digest_maps_items_by_n():
    client = _fake_client('{"items": [{"n": 2, "headline": "B 💡", "text": "тб"}, '
                          '{"n": 1, "headline": "A 🚀", "text": "та"}], "cta": "CTA"}')
    items, cta = notifier.generate_digest(_hot(), client=client)
    assert cta == "CTA"
    assert items[0]["article_index"] == 1
    assert items[1]["article_index"] == 0
    assert items[0]["headline"] == "B 💡"


def test_generate_digest_on_parse_failure_falls_back():
    client = _fake_client("мусор вместо JSON")
    hot = _hot()
    items, cta = notifier.generate_digest(hot, client=client)
    assert [i["headline"] for i in items] == [a["title"] for a in hot]
    assert cta == ""


# --- _parse_factcheck_json / _apply_verdicts ---

def test_parse_factcheck_json_valid():
    raw = '[{"n": 1, "verdict": "ok", "text": "", "query": "OpenAI Sora 2"}]'
    parsed = notifier._parse_factcheck_json(raw)
    assert parsed is not None
    assert parsed[0]["verdict"] == "ok"


def test_parse_factcheck_json_invalid_returns_none():
    assert notifier._parse_factcheck_json("{битый") is None


def test_apply_verdicts_drops_wrong():
    items = [{"headline": "H", "text": "T", "article_index": 0}]
    checks = [{"n": 1, "verdict": "wrong", "text": "", "query": "q"}]
    assert notifier._apply_verdicts(items, checks) == []


def test_apply_verdicts_replaces_uncertain_text():
    items = [{"headline": "H", "text": "старый", "article_index": 0}]
    checks = [{"n": 1, "verdict": "uncertain", "text": "исправленный", "query": "q"}]
    result = notifier._apply_verdicts(items, checks)
    assert result[0]["text"] == "исправленный"


def test_apply_verdicts_keeps_ok():
    items = [{"headline": "H", "text": "старый", "article_index": 0}]
    checks = [{"n": 1, "verdict": "ok", "text": "", "query": "q"}]
    result = notifier._apply_verdicts(items, checks)
    assert result[0]["text"] == "старый"
    assert result[0]["query"] == "q"


# --- corroboration ---

def test_publishers_from_google_news_titles():
    arts = [
        {"title": "OpenAI открыла Sora - Ведомости"},
        {"title": "Sora 2 доступна - РБК"},
        {"title": "Sora снова - Ведомости"},
    ]
    assert notifier._publishers_from_google_news(arts) == {"Ведомости", "РБК"}


def test_corroborate_digest_sets_flag():
    items = [{"headline": "H", "text": "T", "article_index": 0, "query": "OpenAI Sora"}]
    from unittest.mock import patch
    arts = [
        {"title": "Новость - Ведомости", "url": "https://a", "source": "G"},
        {"title": "Новость - РБК", "url": "https://b", "source": "G"},
    ]
    with patch("news_pipeline.fetch_google_news", return_value=arts):
        result = notifier._corroborate_digest(items)
    assert result[0]["corroborated"] is True


def test_corroborate_digest_without_query_skips_search():
    from unittest.mock import patch
    with patch("news_pipeline.fetch_google_news", side_effect=AssertionError("нельзя")):
        result = notifier._corroborate_digest([{"headline": "H", "text": "T", "article_index": 0}])
    assert result[0]["corroborated"] is False


# --- assemble_digest_post ---

def test_assemble_digest_post_structure():
    items = [{"headline": "Sora открыли 💡", "text": "OpenAI открыла доступ.",
              "article_index": 0, "url": "https://openai.com/sora"}]
    post = notifier.assemble_digest_post(items, cta="Кто уже пробовал?")
    assert "📊 Дайджест к этому часу" in post
    assert "Sora открыли 💡" in post
    assert '<a href="https://openai.com/sora">' in post
    assert post.strip().endswith("Кто уже пробовал?")


def test_assemble_digest_post_escapes_html():
    items = [{"headline": "A <b> inj", "text": "x <script>", "article_index": 0,
              "url": "https://a.com/?x=1&y=2"}]
    post = notifier.assemble_digest_post(items, cta=None)
    assert "<b> inj" not in post
    assert "&lt;b&gt;" in post
    assert "x=1&amp;y=2" in post