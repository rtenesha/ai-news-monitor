# Editorial Cards (/digest, /card) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Карточки для редактора по команде `/digest` (список тем) и `/card <№>` (полная карточка §8 с черновиком) в Telegram-боте.

**Architecture:** Новый модуль `editorial.py` (сбор источников, AI-скоринг по 6 критериям макс 12, карточка, фактчекинг черновика) поверх существующего `news_pipeline.py`; переиспользование фактчек-механики из `notifier.py` импортом (не переезжает). Команды `/digest` и `/card` добавляются в `bot.py`; карточки и темы живут в памяти процесса (`bot_data`). Старые системы (`monitor.py`, `notifier.py`) не трогаются.

**Tech Stack:** Python 3, Groq (`openai/gpt-oss-120b`, `reasoning_effort="low"`), pydantic уже есть, feedparser/urllib keyless, pytest (+ pytest-asyncio НЕ нужен — тестируются чистые функции), python-telegram-bot v20.

**Spec:** `docs/superpowers/specs/2026-10-12-editorial-cards-design.md` — план аргументируется от спеки, исполнитель читает оба документа.

## Global Constraints

- Модель везде `openai/gpt-oss-120b`, `reasoning_effort="low"` (llama-4-scout и llama-3.3-70b удалены из Groq).
- Новые источники строго keyless (без API-ключей); только 4 RSS-блога + HN + GitHub Trending + Product Hunt + GitHub Blog + HF trending.
- Ничего не отправляется автоматически: карточки только по команде в боте; `TELEGRAM_AUTO_SEND` не трогается.
- Всё, что попадает в Telegram HTML, экранируется `html.escape(...)` (для href — `quote=True`).
- Критерии оценки: 0–2 каждый (6 штук), сумма 0–12; пороги: ≥9 приоритет, ≥6 в списке, <6 отброшено (в AI-режиме).
- Рубрики строго из списка `RUBRICS`; «Инфоповод-пустышка» = тема отбрасывается; формат строго из `{"А","Б","В","Г","Д","Е"}`.
- Лимиты: капит кандидатов на скоринг 15; тем в списке максимум 8.
- TDD: каждый тест сначала пишется и наблюдается падающим (RED), потом минимальная реализация (GREEN).
- Каждый коммит заканчивается строкой `Co-Authored-By: Claude Code <noreply@anthropic.com>`.

## Review Focus

- **Непроверенный AI-текст в HTML**: заголовки/тела черновиков содержат `<` и `&` чаще, чем RSS-тайтлы — если забыть `html.escape`, Telegram молча отклонит всё сообщение. Ожидание: даже текст «a<b&c» из модели доезжает до канала. → тест в Task 7 (`test_format_card_messages_escapes_html`).
- **Лимит 4096**: карточка §8 — ~20 полей, легко превышает лимит одним сообщением. Ожидание: разбивка по абзацам на ≤4000 знаков, черновик всегда отдельным сообщением. → тест в Task 7 (`test_format_card_messages_splits_long_text`).
- **HF trending без даты**: `published` у записей HF — всегда `None`; если форматтер даты не готов к `None`, карточка падает на `AttributeError`. Ожидание: «трендинг на момент сбора, без даты выхода». → тест в Task 7 (`test_format_card_handles_hf_no_date`).
- **Повторный `/card` на ту же тему**: сжигает квоту Groq. Ожидание: карточка пересобирается только если её нет; повторная команда возвращает сохранённую. → тест в Task 8 (`test_get_or_build_card_reuses_builtin`).
- **`/card` с несуществующим номером** (0, 999, «abc»): Ожидание: приглашение вызвать `/digest`, не traceback. → тест в Task 8 (`test_resolve_topic_bounds`).

---

### Task 1: Фетчер Hugging Face trending в `news_pipeline.py`

**Files:**
- Modify: `news_pipeline.py` (добавить после `fetch_github_trending`, ~строка 349, до `_google_news_time_operator`)
- Test: `tests/test_news_pipeline.py` (добавить в конец)

**Interfaces:**
- Consumes: `_BROWSER_UA`, `track_source_health(name, ok)` — уже есть в `news_pipeline.py`.
- Produces: `fetch_hf_trending(limit: int = 20) -> list[dict]`; статьи вида `{"title", "url", "summary", "source", "published": None}` — `published` всегда `None` (трендинг не привязан к дате); sources: `"HuggingFace: модели"` и `"HuggingFace: Spaces"`.

- [ ] **Step 1: Write the failing test**

Добавить в конец `tests/test_news_pipeline.py` (импорты `io`, `json` уже в файле — проверить, если нет, добавить):

```python
def test_fetch_hf_trending_parses_rows_and_skips_malformed():
    payloads = {
        "https://huggingface.co/api/models?sort=trendingScore&limit=20&direction=-1": [
            {"id": "meta-llama/Llama-4", "likes": 100, "downloads": 5000},
            {"no_id": True},
        ],
        "https://huggingface.co/api/spaces?sort=trendingScore&limit=20&direction=-1": [
            {"id": "demo-cool-space", "likes": 7},
        ],
    }

    class _Resp(io.BytesIO):
        status = 200

    def mock_urlopen(req, timeout=15):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        return _Resp(json.dumps(payloads[url]).encode())

    with patch("urllib.request.urlopen", side_effect=mock_urlopen):
        articles = news_pipeline.fetch_hf_trending()

    assert len(articles) == 2
    assert articles[0]["title"] == "Llama-4 (HF модели)"
    assert articles[0]["url"] == "https://huggingface.co/meta-llama/Llama-4"
    assert articles[0]["source"] == "HuggingFace: модели"
    assert "5000 загрузок" in articles[0]["summary"]
    assert articles[0]["published"] is None
    assert articles[1]["source"] == "HuggingFace: Spaces"
    assert articles[1]["summary"].startswith("Трендинг на Hugging Face")


def test_fetch_hf_trending_never_raises_on_network_failure():
    with patch("urllib.request.urlopen", side_effect=RuntimeError("blocked")):
        assert news_pipeline.fetch_hf_trending() == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_news_pipeline.py -k hf_trending -v`
Expected: FAIL — `AttributeError: module 'news_pipeline' has no attribute 'fetch_hf_trending'`

- [ ] **Step 3: Write minimal implementation**

В `news_pipeline.py` после функции `fetch_github_trending` (перед `def _google_news_time_operator`):

```python
HF_TRENDING_URL = "https://huggingface.co/api/{kind}?sort=trendingScore&limit={limit}&direction=-1"


def fetch_hf_trending(limit: int = 20) -> list[dict]:
    """Hugging Face trending models and Spaces via the keyless public API.
    Not a timed feed — reflects what is trending at fetch moment, so
    `published` stays None (collectors must not window-filter these).
    Never raises; rows without an id are skipped."""
    articles = []
    for kind, label in (("models", "модели"), ("spaces", "Spaces")):
        url = HF_TRENDING_URL.format(kind=kind, limit=limit)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _BROWSER_UA})
            with urllib.request.urlopen(req, timeout=15) as resp:
                rows = json.loads(resp.read().decode("utf-8", errors="ignore"))
        except Exception as e:
            print(f"HF trending error [{kind}]: {e}")
            track_source_health(f"HuggingFace: {kind}", ok=False)
            continue
        if not isinstance(rows, list):
            track_source_health(f"HuggingFace: {kind}", ok=False)
            continue
        for row in rows:
            if not isinstance(row, dict) or not row.get("id"):
                continue
            name = str(row["id"])
            title = name.split("/", 1)[1] if "/" in name else name
            stats = []
            if row.get("downloads"):
                stats.append(f"{row['downloads']} загрузок")
            if row.get("likes"):
                stats.append(f"{row['likes']} звёзд")
            summary = "Трендинг на Hugging Face: " + ", ".join(stats) if stats \
                else "Трендинг на Hugging Face."
            articles.append({
                "title": f"{title} (HF {label})",
                "url": f"https://huggingface.co/{name}",
                "summary": summary,
                "source": f"HuggingFace: {label}",
                "published": None,
            })
        track_source_health(f"HuggingFace: {kind}", ok=True)
    return articles
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_news_pipeline.py -k hf_trending -v`
Expected: 2 PASS

- [ ] **Step 5: Commit**

```bash
git add news_pipeline.py tests/test_news_pipeline.py
git commit -m "$(cat <<'EOF'
Pipeline: Hugging Face trending models/spaces fetcher (keyless)

Co-Authored-By: Claude Code <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Каркас `editorial.py` — константы, keyword-фильтр, сбор тем

**Files:**
- Create: `editorial.py`
- Test: `tests/test_editorial.py` (создать)

**Interfaces:**
- Consumes: `news_pipeline.fetch_rss_feed(name, url, hours) -> (articles, ok)`; `news_pipeline.fetch_hackernews(hours) -> list[dict]`; `news_pipeline.fetch_github_trending(hours) -> list[dict]`; `news_pipeline.fetch_hf_trending() -> list[dict]` (Task 1); `news_pipeline.dedup_cross_source(articles) -> list[dict]`; из `notifier`: `KEYWORDS`, `HIGH_VALUE`, `_kw_matches(kw, text) -> bool`, `score_article(article) -> int`.
- Produces: `EDITORIAL_FEEDS: list[dict]` (6 фидов), `CANDIDATE_CAP = 15`, `TOPIC_LIST_MAX = 8`, `PRIORITY_TOTAL = 9`, `LIST_TOTAL = 6`, `RUBRICS: list[str]` (8 стр.), `POST_FORMATS: dict[str, str]` (А–Е); `collect_topics(hours: int = 24) -> list[dict]` — отсортированные по keyword-баллу капнутые кандидаты.

Примечание к спеке: спека писала «копия KEYWORDS» — импорт из `notifier` даёт тот же результат DRY-нее (один источник правды; `bot.py` уже импортирует `notifier`). Отклонение осознанное.

- [ ] **Step 1: Write the failing test**

Создать `tests/test_editorial.py`:

```python
"""Tests for editorial.py: topic collection, 6-criteria AI scoring,
editor cards per the 2026-10-12 spec, card fact-checking, formatting."""

from unittest.mock import Mock, patch

import editorial
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
```

```python
# --- collect_topics ---

def test_collect_topics_merges_sources_dedups_and_caps():
    feeds = {f["name"]: ([_article(summary="ИИ нейросеть ChatGPT")], True)
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
    weak = [_article("слабая ИИ заметка", url="https://a.example/w1")]
    strong = [_article("ChatGPT Claude GPT LLMskills промты", url="https://a.example/s2")]
    with patch.object(news_pipeline, "fetch_rss_feed",
                      return_value=(weak + strong, True)), \
         patch.object(news_pipeline, "fetch_hackernews", return_value=[]), \
         patch.object(news_pipeline, "fetch_github_trending", return_value=[]), \
         patch.object(news_pipeline, "fetch_hf_trending", return_value=[]):
        topics = editorial.collect_topics(hours=24)

    assert topics[0]["url"] == "https://a.example/s2"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_editorial.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'editorial'`

- [ ] **Step 3: Write minimal implementation**

Создать `editorial.py`:

```python
#!/usr/bin/env python3
"""Editorial cards per the 2026-10-12 editor instruction: topic collection
from keyless sources, 6-criteria AI scoring (max 12), editor cards with
draft + fact-check. Spec: docs/superpowers/specs/2026-10-12-editorial-cards-design.md."""

import html
import json
import re
from datetime import datetime, timezone

import news_pipeline
from notifier import (KEYWORDS, HIGH_VALUE, _kw_matches, score_article,
                      clean_text, _NATURAL_TEXT_RULES, _CONTENT_FORMULA,
                      _parse_factcheck_json, _publishers_from_google_news)

MODEL = "openai/gpt-oss-120b"

EDITORIAL_FEEDS = [
    {"name": "OpenAI Blog", "url": "https://openai.com/news/rss.xml"},
    {"name": "Google AI Blog", "url": "https://blog.google/technology/ai/rss/"},
    {"name": "DeepMind Blog", "url": "https://deepmind.google/blog/rss.xml"},
    {"name": "HuggingFace Blog", "url": "https://huggingface.co/blog/feed.xml"},
    {"name": "Product Hunt", "url": "https://www.producthunt.com/feed?category=artificial-intelligence"},
    {"name": "GitHub Blog", "url": "https://github.blog/feed/"},
]

CANDIDATE_CAP = 15
TOPIC_LIST_MAX = 8
PRIORITY_TOTAL = 9  # 9-12 — приоритет
LIST_TOTAL = 6      # 6-8 — резерв; <6 — отброшено (в AI-режиме)
FALLBACK_TOTAL = 1  # порог списка в режиме без AI

RUBRICS = [
    "Новое в ИИ", "Инструменты и приложения", "Хаки и лайфхаки",
    "Промты и скилы", "Ресёрч и демо", "Industry/рынок и деньги",
    "Обучение и навыки", "Инфоповод-пустышка",
]
REJECT_RUBRIC = "Инфоповод-пустышка"  # сюда попавшее отбрасывается

POST_FORMATS = {
    "А": "короткая новость или перевод",
    "Б": "обзор с деталями и выводом",
    "В": "туториал / пошаговый разбор",
    "Г": "подборка (список)",
    "Д": "«так можно было?!» — лайфхак от лица канала",
    "Е": "реакция/мнение",
}


def collect_topics(hours: int = 24) -> list[dict]:
    """Gather candidates from all editorial sources: dedup, keyword filter,
    cap by keyword score. Never raises (source fetchers are safe individually)."""
    articles: list[dict] = []
    for feed in EDITORIAL_FEEDS:
        posts, _ok = news_pipeline.fetch_rss_feed(feed["name"], feed["url"], hours)
        articles += posts
    articles += news_pipeline.fetch_hackernews(hours=hours)
    articles += news_pipeline.fetch_github_trending(hours=hours)
    articles += news_pipeline.fetch_hf_trending()
    articles = news_pipeline.dedup_cross_source(articles)
    relevant = [
        a for a in articles
        if any(_kw_matches(kw, (a["title"] + " " + a["summary"]).lower())
               for kw in KEYWORDS)
    ]
    return sorted(relevant, key=score_article, reverse=True)[:CANDIDATE_CAP]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_editorial.py -v`
Expected: 3 PASS

- [ ] **Step 5: Commit**

```bash
git add editorial.py tests/test_editorial.py
git commit -m "$(cat <<'EOF'
Editorial: topic collection from keyless sources (PH, GitHub Blog, HF)

Co-Authored-By: Claude Code <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Редакторский скоринг `score_topic_ai` (6 критериев, макс 12)

**Files:**
- Modify: `editorial.py`
- Test: `tests/test_editorial.py` (добавить)

**Interfaces:**
- Consumes: `clean_text` из notifier; фейковый клиент из теста.
- Produces: `CRITERIA = ["новизна", "польза", "удивление", "актуальность", "пересылка", "соответствие"]`; `score_topic_ai(article: dict, groq_client, full_text: str | None = None) -> dict | None`; возвращает `{"criteria": {<критерий>: 0-2}, "total": <сумма 0-12 пересчитана кодом>, "rubric": str (валидная или None→отброс), "post_format": "А".."Е", "one_liner": str}`; `None` при любой ошибке (вызов / битый JSON / невалидные поля / рубрика-пустышка).

- [ ] **Step 1: Write the failing test**

```python
# --- score_topic_ai ---

def _score_raw(total_note="10"):
    return ('{"criteria": {"новизна": 2, "польза": 2, "удивление": 1, '
            '"актуальность": 2, "пересылка": 1, "соответствие": 2}, '
            '"total": ' + total_note + ', "rubric": "Инструменты и приложения", '
            '"post_format": "Б", "one_liner": "Скилы ставятся сами"}')


def test_score_topic_ai_parses_valid_json():
    result = editorial.score_topic_ai(_article(), _fake_client(_score_raw()))
    assert result is not None
    assert result["total"] == 10
    assert result["criteria"]["новизна"] == 2
    assert result["rubric"] == "Инструменты и приложения"
    assert result["post_format"] == "Б"
    assert result["one_liner"] == "Скилы ставятся сами"


def test_score_topic_ai_recomputes_total_from_criteria():
    # модель соврала про total=12, сумма критериев 10 — код пересчитывает
    result = editorial.score_topic_ai(_article(), _fake_client(_score_raw("12")))
    assert result["total"] == 10


def test_score_topic_ai_rejects_out_of_range_criteria():
    raw = '{"criteria": {"новизна": 3, "польза": 2, "удивление": 1, "актуальность": 2, "пересылка": 2, "соответствие": 0}, "tot'
    raw += 'al": 10, "rubric": "Новое в ИИ", "post_format": "А", "one_liner": "x"}'
    assert editorial.score_topic_ai(_article(), _fake_client(raw)) is None


def test_score_topic_ai_rejects_unknown_rubric_and_format():
    bad_rubric = _score_raw().replace("Инструменты и приложения", "Финансы")
    assert editorial.score_topic_ai(_article(), _fake_client(bad_rubric)) is None
    bad_fmt = _score_raw().replace('"Б"', '"Ж"')
    assert editorial.score_topic_ai(_article(), _fake_client(bad_fmt)) is None


def test_score_topic_ai_drops_reject_rubric():
    raw = _score_raw().replace("Инструменты и приложения", "Инфоповод-пустышка")
    assert editorial.score_topic_ai(_article(), _fake_client(raw)) is None


def test_score_topic_ai_none_on_garbage_or_exception():
    assert editorial.score_topic_ai(_article(), _fake_client("не JSON вообще")) is None
    client = Mock()
    client.chat.completions.create.side_effect = RuntimeError("down")
    assert editorial.score_topic_ai(_article(), client) is None
    assert editorial.score_topic_ai(_article(), None) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_editorial.py -k score_topic -v`
Expected: FAIL — `AttributeError: module 'editorial' has no attribute 'score_topic_ai'`

- [ ] **Step 3: Write minimal implementation**

Добавить в `editorial.py` (после `collect_topics`):

```python
CRITERIA = ["новизна", "польза", "удивление", "актуальность",
            "пересылка", "соответствие"]

_CRITERIA_HELP = (
    "- новизна: произошло только что, о таком ещё не рассказывали\n"
    "- польза: читатель может применить сам прямо сейчас\n"
    "- удивление: эффект «так можно было?!»\n"
    "- актуальность: это обсуждается прямо сейчас\n"
    "- пересылка: захочется переслать коллеге\n"
    "- соответствие: про ИИ и вайбкодинг, а не общий маркетинг без ИИ-угла"
)

_SCORE_SYSTEM_PROMPT = (
    "Ты — отбирающий редактор Telegram-канала Zerocoder. Пиши исключительно на "
    "русском языке. Отвечай СТРОГО одним валидным JSON-объектом, без пояснений."
)


def _extract_json_object(raw) -> dict | None:
    """First {...} from raw model output, tolerating ```json fences/prose."""
    if not raw:
        return None
    text = re.sub(r"```(?:json)?|```", "", str(raw))
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _parse_topic_score(raw) -> dict | None:
    """Validate scorer JSON: criteria 0-2 each, rubric and format from known
    lists. total is RECOMPUTED from criteria (model's own number ignored).
    Рублика «Инфоповод-пустышка» = тема не годится → None (отброс)."""
    data = _extract_json_object(raw)
    if not data:
        return None
    given = data.get("criteria")
    if not isinstance(given, dict):
        return None
    criteria = {}
    for key in CRITERIA:
        v = given.get(key)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= int(v) <= 2:
            return None
        criteria[key] = int(v)
    rubric = str(data.get("rubric", "")).strip()
    fmt = str(data.get("post_format", data.get("format", ""))).strip().upper()
    one_liner = clean_text(str(data.get("one_liner", ""))).strip()
    if rubric not in RUBRICS or fmt not in POST_FORMATS or not one_liner:
        return None
    if rubric == REJECT_RUBRIC:
        return None
    return {"criteria": criteria, "total": sum(criteria.values()),
            "rubric": rubric, "post_format": fmt, "one_liner": one_liner}


def score_topic_ai(article: dict, groq_client, full_text=None) -> dict | None:
    """Editorial 6-criteria scoring of one article (max 12). Returns None on
    any failure — caller falls back to keyword scoring for this article."""
    if not groq_client:
        return None
    content = full_text or article["summary"]
    prompt = (
        "Оцени материал для канала об ИИ и вайбкодинге по 6 критериям — каждый "
        "0, 1 или 2 (2 — сильно, 0 — нет):\n\n"
        f"{_CRITERIA_HELP}\n\n"
        f"Выбери ОДНУ рубрику строго из списка: {'; '.join(RUBRICS)}. "
        f"«Инфоповод-пустышка» — если материал не годится для поста.\n\n"
        "Выбери формат поста, который лучше всего подойдёт:\n"
        + "\n".join(f"- {k}: {v}" for k, v in POST_FORMATS.items())
        + "\n\nОдной строкой опиши суть материала (one_liner).\n\n"
        "Ответь строго одним JSON-объектом:\n"
        '{"criteria": {"новизна": <0-2>, "польза": <0-2>, "удивление": <0-2>, '
        '"актуальность": <0-2>, "пересылка": <0-2>, "соответствие": <0-2>}, '
        '"total": <сумма>, "rubric": "<из списка>", "post_format": "<А-Е>", '
        '"one_liner": "<одна строка>"}\n\n'
        f"Источник: {article['source']}\n"
        f"Заголовок: {article['title']}\n"
        f"Содержание: {content[:3000]}"
    )
    try:
        response = groq_client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": _SCORE_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            max_tokens=2000,
            reasoning_effort="low",
        )
    except Exception:
        return None
    return _parse_topic_score(response.choices[0].message.content)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_editorial.py -v`
Expected: все PASS (включая Task 2)

- [ ] **Step 5: Commit**

```bash
git add editorial.py tests/test_editorial.py
git commit -m "$(cat <<'EOF'
Editorial: 6-criteria structured topic scoring (max 12, thresholds next)

Co-Authored-By: Claude Code <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Отбор тем `select_topics` (пороги, сортировка, режим без AI)

**Files:**
- Modify: `editorial.py`
- Test: `tests/test_editorial.py` (добавить)

**Interfaces:**
- Consumes: `score_topic_ai` (Task 3), `score_article` (notifier), `news_pipeline.extract_full_text(url) -> str | None`, `news_pipeline.fetch_hf_trending` уже подключён.
- Produces: `select_topics(articles: list[dict], groq_client=None) -> tuple[list[dict], bool]`; topic = `{"article": dict, "criteria": dict, "total": int, "rubric": str, "post_format": str, "one_liner": str, "ai": bool}`; второй элемент — `used_ai: bool`. AI-режим: только `total >= LIST_TOTAL`, сортировка по total↓ затем по дате↓ (published None = самая старая 1970). Фолбэк: `total = score_article(...)`, топ-8 с `total >= 1`. Статьи в AI-режиме получают `article["_full_text"]`.

- [ ] **Step 1: Write the failing test**

```python
# --- select_topics ---

def test_select_topics_ai_mode_keeps_total_ge_6_and_sorts():
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
    arts = [_article("ChatGPT новость", url="https://a.example/1")]
    client = Mock()
    client.chat.completions.create.side_effect = RuntimeError("down")
    with patch.object(news_pipeline, "extract_full_text", return_value=None):
        topics, used_ai = editorial.select_topics(arts, client)

    assert used_ai is True  # клиент был, но конкретная статья ушла в фолбэк
    assert topics[0]["ai"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_editorial.py -k select_topics -v`
Expected: FAIL — `no attribute 'select_topics'`

- [ ] **Step 3: Write minimal implementation**

```python
_OLD_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _topic_sort_key(topic: dict):
    return (topic["total"], topic["article"].get("published") or _OLD_EPOCH)


def select_topics(articles: list[dict], groq_client=None) -> tuple[list[dict], bool]:
    """Score candidates into editor topics. AI mode: total >= LIST_TOTAL.
    Fallback mode (no client): keyword totals, marked ai=False and capped
    at TOPIC_LIST_MAX with total >= FALLBACK_TOTAL."""
    used_ai = bool(groq_client)
    topics = []
    for article in articles:
        full_text = None
        if groq_client:
            full_text = news_pipeline.extract_full_text(article["url"])
            article["_full_text"] = full_text
        result = score_topic_ai(article, groq_client, full_text=full_text)
        if result:
            topic = {"article": article, **result, "ai": True}
        else:
            topic = {"article": article, "criteria": {},
                     "total": score_article(article),
                     "rubric": "не определена", "post_format": "",
                     "one_liner": article["title"], "ai": False}
        topics.append(topic)

    if used_ai:
        topics = [t for t in topics if t["total"] >= LIST_TOTAL]
    else:
        topics = [t for t in topics if t["total"] >= FALLBACK_TOTAL]
    topics.sort(key=_topic_sort_key, reverse=True)
    return topics[:TOPIC_LIST_MAX], used_ai
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_editorial.py -v`
Expected: все PASS

- [ ] **Step 5: Commit**

```bash
git add editorial.py tests/test_editorial.py
git commit -m "$(cat <<'EOF'
Editorial: topic selection — thresholds 6/8/12, no-AI fallback mode

Co-Authored-By: Claude Code <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: Карточка `build_card` (один AI-вызов, JSON §8)

**Files:**
- Modify: `editorial.py`
- Test: `tests/test_editorial.py` (добавить)

**Interfaces:**
- Consumes: topic (Task 4), `clean_text`, `_NATURAL_TEXT_RULES`, `_CONTENT_FORMULA`, фейковый клиент.
- Produces: `build_card(topic: dict, groq_client=None) -> dict` — карточка с ключами: `topic_title, rubric, total, criteria, post_format, summary, why_interesting, for_whom, first_source_url, risks, headline_variants: list[str] (до 3), visual_idea, draft_headline, draft_body, draft_cta, claims: list[str] (до 6), confirmed: list[str], needs_check: list[str], sources: list[dict{title,url}], truncated: bool, no_ai: bool`. Без клиента (или при ошибке AI) — метадата-карточка: только `topic_title/rubric/total/summary` из статьи, `no_ai=True`, черновика нет.

- [ ] **Step 1: Write the failing test**

```python
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
           "body": "Claude Code сам нашёл нужный скил и подключил его.\n\nРаньше делали руками.",
           "cta": "Кто-то уже пробовал?"},
 "claims": ["Claude Code сам находит скил", "Раньше подключали руками"]'''
_CARD_RAW += "\n}"
```

```python
def _topic(full_text="Claude Code сам подключает скилы. Раньше было руками."):
    art = _article()
    art["_full_text"] = full_text
    return {"article": art, "criteria": {"новизна": 2}, "total": 11,
            "rubric": "Инструменты и приложения", "post_format": "Б",
            "one_liner": "суть", "ai": True}


def test_build_card_parses_full_json_card():
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
    topic = _topic()
    stub = editorial.build_card(topic, None)
    assert stub["no_ai"] is True
    assert stub["topic_title"] == topic["article"]["title"]
    assert stub["draft_body"] == ""
    assert stub["first_source_url"] == topic["article"]["url"]

    bad = editorial.build_card(topic, _fake_client("не JSON"))
    assert bad["no_ai"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_editorial.py -k build_card -v`
Expected: FAIL — `no attribute 'build_card'`

- [ ] **Step 3: Write minimal implementation**

```python
MAX_CLAIMS = 6

_CARD_SYSTEM_PROMPT = (
    "Ты — редактор Telegram-канала Zerocoder (ИИ и вайбкодинг). Пиши только на "
    "русском языке. Ничего не выдумывай: используешь ТОЛЬКО факты из материала "
    "ниже. Отвечай СТРОГО одним валидным JSON-объектом, без пояснений."
)


def _parse_card_json(raw) -> dict | None:
    """Validate builder JSON per spec §8 fields. Returns None on any failure."""
    data = _extract_json_object(raw)
    if not data:
        return None
    topic_title = clean_text(str(data.get("topic_title", ""))).strip()
    summary = clean_text(str(data.get("summary", ""))).strip()
    draft = data.get("draft")
    if not topic_title or not summary or not isinstance(draft, dict):
        return None
    draft_body = clean_text(str(draft.get("body", ""))).strip()
    if not draft_body:
        return None
    variants = data.get("headline_variants")
    if not isinstance(variants, list):
        variants = []
    variants = [clean_text(str(v)).strip() for v in variants][:3]
    variants = [v for v in variants if v]
    claims = data.get("claims")
    if not isinstance(claims, list):
        claims = []
    claims = [clean_text(str(c)).strip() for c in claims][:MAX_CLAIMS]
    claims = [c for c in claims if c]
    return {
        "topic_title": topic_title,
        "summary": summary,
        "why_interesting": clean_text(str(data.get("why_interesting", ""))).strip(),
        "for_whom": clean_text(str(data.get("for_whom", ""))).strip(),
        "risks": clean_text(str(data.get("risks", ""))).strip(),
        "headline_variants": variants,
        "visual_idea": clean_text(str(data.get("visual_idea", ""))).strip(),
        "draft_headline": clean_text(str(draft.get("headline", ""))).strip(),
        "draft_body": draft_body,
        "draft_cta": clean_text(str(draft.get("cta", ""))).strip(),
        "claims": claims,
    }


def _card_stub(topic: dict) -> dict:
    article = topic["article"]
    return {
        "topic_title": article["title"],
        "rubric": topic.get("rubric", "не определена"),
        "total": topic["total"], "criteria": topic.get("criteria", {}),
        "post_format": topic.get("post_format", ""),
        "summary": article.get("summary", "")[:600],
        "why_interesting": "", "for_whom": "",
        "first_source_url": article["url"],
        "risks": "Материал не прочитан полностью (нет AI) — факты не проверены.",
        "headline_variants": [], "visual_idea": "",
        "draft_headline": "", "draft_body": "", "draft_cta": "",
        "claims": [], "confirmed": [], "needs_check": [], "sources": [],
        "truncated": False, "no_ai": True,
    }


def build_card(topic: dict, groq_client=None) -> dict:
    """Editor card for one topic (spec §8). With a client: one AI call over
    the full text; without (or on failure) — metadata-only stub."""
    if not groq_client:
        return _card_stub(topic)
    article = topic["article"]
    content = article.get("_full_text") or article.get("summary", "")
    numbered_claims = ("\n".join(f"{i + 1}. {c}" for i, c in enumerate(claims))
                       if (claims := card_json and card_json.get("claims")) else "")
    prompt = (
        "Собери редакторскую карточку по материалу ниже.\n\n"
        f"Рубрика (уже выбрана скорингом): {topic['rubric']}\n"
        f"Формат поста (выбран скорингом): {topic['post_format']}\n\n"
        f"{_NATURAL_TEXT_RULES}\n\n{_CONTENT_FORMULA}\n\n"
        "Черновик публикации: заголовок до 10 слов с эмодзи (не 🤖 и не 🧠), "
        "тело 3-4 коротких абзаца — обязателен конкретный факт (кто и что сделал), "
        "практический вывод для читателя; CTA — один лёгкий вопрос без нажима. "
        "Черновик НЕ повторяет заголовок темы и не содержит ссылок.\n\n"
        "Ответь строго одним JSON-объектом:\n"
        '{"topic_title": "<название темы>", "rubric": "<та же рубрика>", '
        '"summary": "<2-4 предложения сути>", "why_interesting": "<почему интересно>", '
        '"for_whom": "<для кого полезно>", "risks": "<ограничения и риски: что может быть '
        'маркетингом, что не проверено>", '
        '"headline_variants": ["<информативный>", "<польза>", "<любопытство>"], '
        '"post_format": "<формат>", "visual_idea": "<одна фраза>", '
        '"draft": {"headline": "...", "body": "...", "cta": "..."}, '
        '"claims": ["<утверждение 1>", "<утверждение 2>"] — все проверяемые факты черновика '
        'по одному в строке, максимум 6}\n\n'
        f"Источник: {article['source']}\n"
        f"Заголовок: {article['title']}\n"
        f"Содержание: {content[:3500]}"
    )
    try:
        response = groq_client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": _CARD_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            max_tokens=3000,
            reasoning_effort="low",
        )
        parsed = _parse_card_json(response.choices[0].message.content)
    except Exception:
        parsed = None

    if not parsed:
        return _card_stub(topic)

    card = _card_stub(topic)
    card.update(parsed)
    card["first_source_url"] = article["url"]
    card["truncated"] = len(content) >= 3500
    card["no_ai"] = False
    return card
```

Внимание при наборе: строка `numbered_claims = ...` из черновика не нужна —
удалить её целиком (утверждения достраиваются фактчекером в Task 6, а не тут).

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_editorial.py -k build_card -v`
Expected: PASS (2 теста)

- [ ] **Step 5: Commit**

```bash
git add editorial.py tests/test_editorial.py
git commit -m "$(cat <<'EOF'
Editorial: build_card — AI card per spec section 8 with draft + claims

Co-Authored-By: Claude Code <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: Фактчекинг карточки `fact_check_card`

**Files:**
- Modify: `editorial.py`
- Test: `tests/test_editorial.py` (добавить)

**Interfaces:**
- Consumes: card (Task 5), `notifier._parse_factcheck_json(raw) -> list[dict{n,verdict,text,query}]` (вердикты {ok,uncertain,wrong}), `notifier._publishers_from_google_news(articles) -> set[str]`, `news_pipeline.fetch_google_news(queries, hours) -> list[dict]`, `news_pipeline.normalize_url(url) -> str`.
- Produces: `fact_check_card(card: dict, article: dict, groq_client=None) -> dict` — мутирует и возвращает карточку: `confirmed` = строки «Подтверждённые факты», `needs_check` = строки «Что требует проверки» (корроборированные с префиксом `✓ (подтверждено другими СМИ): `), `sources` = до 3 доп. источников (`{"title", "url"}`, отличные от URL статьи), черновик **исправляется** (`draft_body` только подтверждённые факты). Без клиента: `confirmed=[]`, `needs_check = claims`, черновик не меняется.

**Соглашение о форме ответа фактчекера** (новый парсер, вердикты — та же схема, что в digest): модель возвращает ОДИН JSON-объект `{"verdicts": [...], "headline": "<исправленный заголовок черновика или пусто>", "body": "<исправленное тело черновика>", "cta": "<исправленный CTA или пусто>"}`, где `verdicts` — массив того же вида, что парсит `notifier._parse_factcheck_json`. Переиспользуем парсер через json.dumps массива.

- [ ] **Step 1: Write the failing test**

```python
# --- fact_check_card ---

def _partially_checked_raw():
    verdicts = ('{"verdicts": ['
                '{"n": 1, "verdict": "ok", "text": "Claude Code сам находит скил", "query": "Claude Code skills"}, '
                '{"n": 2, "verdict": "uncertain", "text": "Переход на автоскилы был раньше", "query": "Claude skills авто"}'
                '], "headline": "", "body": "Claude Code сам находит скил. Автоскилы пришли ранее.", "cta": ""}')
    return str(verdicts)


def test_fact_check_card_splits_verdicts_and_fixes_draft():
    card = editorial.build_card(_topic(), _fake_client(_CARD_RAW))
    article = _article()
    article["_full_text"] = "Claude Code сам подключает скилы."
    checked = editorial.fact_check_card(card, article, _fake_client(_partially_checked_raw()))

    assert checked["confirmed"] == ["Claude Code сам находит скил"]
    assert len(checked["needs_check"]) == 1
    assert "Переход на автоскилы был раньше" in checked["needs_check"][0]
    assert "Автоскилы пришли ранее" in checked["draft_body"]
    assert "Раньше делали руками" not in checked["draft_body"]


def test_fact_check_card_needs_no_client():
    card = editorial.build_card(_topic(), _fake_client(_CARD_RAW))
    original = card["draft_body"]
    checked = editorial.fact_check_card(card, _article(), None)

    assert checked["confirmed"] == []
    assert checked["needs_check"] == card["claims"]
    assert checked["draft_body"] == original


def test_fact_check_card_garbage_keeps_claims_unchecked():
    card = editorial.build_card(_topic(), _fake_client(_CARD_RAW))
    original = card["draft_body"]
    checked = editorial.fact_check_card(card, _article(title="x"), _fake_client("не JSON"))

    assert checked["confirmed"] == []
    assert checked["needs_check"] == card["claims"]
    assert checked["draft_body"] == original
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_editorial.py -k fact_check_card -v`
Expected: FAIL — `no attribute 'fact_check_card'`

- [ ] **Step 3: Write minimal implementation**

```python
_CARD_FC_SYSTEM_PROMPT = (
    "Ты — фактчекер Telegram-канала. Пиши только на русском языке. "
    "Отвечай СТРОГО одним валидным JSON-объектом, без пояснений."
)


def _parse_card_factcheck(raw) -> dict | None:
    """{verdicts: [...same shape as digest...], headline, body, cta} or None."""
    data = _extract_json_object(raw)
    if not data or not isinstance(data.get("verdicts"), list):
        return None
    checks = _parse_factcheck_json(json.dumps(data["verdicts"]))
    return {"verdicts": checks, "headline": str(data.get("headline", "")).strip(),
            "body": str(data.get("body", "")).strip(),
            "cta": str(data.get("cta", "")).strip()}


def fact_check_card(card: dict, article: dict, groq_client=None) -> dict:
    """Fact-check the draft's numbered claims against the article text
    (reusing the digest fact-check verdict scheme), then corroboration via
    Google News. wrong claims leave the draft; uncertain land in needs_check."""
    claims = card.get("claims", [])
    if not groq_client or not claims:
        card["confirmed"] = []
        card["needs_check"] = list(claims)
        return card

    full_text = article.get("_full_text") or article.get("summary", "")
    numbered = "\n".join(f"{i + 1}. {claim}" for i, claim in enumerate(claims))
    prompt = (
        "Сверь каждое утверждение черновика с текстом статьи:\n"
        "- «ok» — подтверждается текстом статьи.\n"
        "- «uncertain» — не подтверждается, но и не противоречит.\n"
        "- «wrong» — противоречит тексту статьи.\n\n"
        "В «body» верни тело черновика, где wrong-утверждения удалены, а "
        "uncertain переформулированы только из подтверждённого (пусто — без правок).\n"
        "Для каждого утверждения — query: короткий поисковый запрос 2-4 слова.\n\n"
        "Ответь строго одним JSON-объектом:\n"
        '{"verdicts": [{"n": <номер>, "verdict": "ok|uncertain|wrong", '
        '"text": "<верифицированная формулировка>", "query": "<запрос>"}], '
        '"headline": "<исправленный заголовок или пусто>", '
        '"body": "<исправленное тело или пусто>", "cta": "<исправленный CTA или пусто>"}\n\n'
        f"Текст статьи: {full_text[:1800]}\n"
        f"(оригинал: {article.get('url', '')})\n\n"
        "Утверждения черновика:\n" + numbered
    )
    try:
        response = groq_client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": _CARD_FC_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            max_tokens=2000,
            reasoning_effort="low",
        )
        data = _parse_card_factcheck(response.choices[0].message.content)
    except Exception:
        data = None

    if not data or not data["verdicts"]:
        card["confirmed"] = []
        card["needs_check"] = list(claims)
        return card

    by_n = {c["n"]: c for c in data["verdicts"]}
    confirmed, needs_check, sources, seen = [], [], [], set()
    own_key = news_pipeline.normalize_url(article.get("url", ""))
    for i, claim in enumerate(claims):
        check = by_n.get(i + 1)
        if check is None or check["verdict"] == "wrong":
            continue
        text = check["text"] or claim
        if check["verdict"] == "ok":
            confirmed.append(text)
            continue
        # uncertain — корроборация через Google News
        entry = {"text": text, "query": check["query"]}
        try:
            found = news_pipeline.fetch_google_news([check["query"]], hours=24) \
                if check["query"] else []
        except Exception:
            found = []
        publishers = _publishers_from_google_news(found)
        corroborated = len(publishers) >= 2
        for a in found:
            key = news_pipeline.normalize_url(a["url"])
            if key != own_key and key not in seen:
                seen.add(key)
                sources.append({"title": a["title"], "url": a["url"]})
            if len(sources) >= 3:
                break
        label = "✓ (подтверждено другими СМИ): " if corroborated else ""
        needs_check.append(label + text)

    card["confirmed"] = confirmed
    card["needs_check"] = needs_check
    card["sources"] = card.get("sources", []) + sources[:3]
    if data["body"]:
        card["draft_body"] = clean_text(data["body"])
    if data["headline"]:
        card["draft_headline"] = clean_text(data["headline"])
    if data["cta"]:
        card["draft_cta"] = clean_text(data["cta"])
    return card
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_editorial.py -k fact_check_card -v`
Expected: PASS (3 теста)

- [ ] **Step 5: Commit**

```bash
git add editorial.py tests/test_editorial.py
git commit -m "$(cat <<'EOF'
Editorial: card fact-check reusing digest verdict scheme + corroboration

Co-Authored-By: Claude Code <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: Форматирование — `render_topic_list`, `format_card_messages`, разбивка

**Files:**
- Modify: `editorial.py`
- Test: `tests/test_editorial.py` (добавить)

**Interfaces:**
- Consumes: card (Task 5/6), topic (Task 4), `html.escape`.
- Produces: `render_topic_list(topics, hours: int, used_ai: bool) -> str` (один HTML-текст списка); `format_card_messages(card: dict) -> list[str]` — 1+ сообщений Telegram HTML (карточка целиком, черновик — последнее отдельное сообщение), каждое ≤ 4096 знаков; `_split_by_paragraphs(text, limit=4000) -> list[str]`.

- [ ] **Step 1: Write the failing test**

```python
# --- форматирование ---

def test_render_topic_list_numbers_and_marks_priority():
    topics = [_topic(), dict(_topic(), total=7)]
    text = editorial.render_topic_list(topics, 24, used_ai=True)

    assert "1." in text and "2." in text
    assert "/card 1" in text and "/card 2" in text
    assert "11/12" in text and "7/12" in text
    assert "приоритет" in text and "резерв" in text


def test_render_topic_list_warns_without_ai():
    text = editorial.render_topic_list([_topic()], 24, used_ai=False)
    assert "без AI" in text


def test_format_card_messages_escapes_html():
    card = editorial.build_card(_topic(), _fake_client(_CARD_RAW))
    card["summary"] = "скрипт <b>жирный</b> & хак"
    card["draft_body"] = "тело <script>alert(1)&</script>"
    msgs = editorial.format_card_messages(card)

    assert "<b>жирный</b>" not in msgs[0]
    assert "&lt;b&gt;жирный&lt;/b&gt;" in msgs[0]
    draft_msg = [m for m in msgs if " Черновик" in m][0]
    assert "&lt;script&gt;" in draft_msg
    assert all(len(m) <= 4096 for m in msgs)


def test_format_card_messages_keeps_draft_separate():
    card = editorial.build_card(_topic(), _fake_client(_CARD_RAW))
    msgs = editorial.format_card_messages(card)
    assert "draft_body" not in msgs[0]
    assert card["draft_body"] in msgs[-1]
    assert "Черновик" in msgs[-1]


def test_format_card_messages_splits_long_body():
    card = editorial.build_card(_topic(), _fake_client(_CARD_RAW))
    card["draft_body"] = "абзац один\n\n" * 700  # ~7000 знаков
    msgs = editorial.format_card_messages(card)
    assert all(len(m) <= 4096 for m in msgs)
    assert len(msgs) > 2


def test_format_card_handles_hf_no_date():
    card = editorial.build_card(
        dict(_topic(), article=_article(published=None)), _fake_client(_CARD_RAW))
    card["published"] = None
    msgs = editorial.format_card_messages(card)
    assert "трендинг на момент сбора" in msgs[0] or "без даты" in msgs[0]
```

(При наборе исправить опечатку «Ранше»→«Раньше» в `_topic.full_text`, если она
пролезет из Task 5 — тест build_card уже должен был её поймать.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_editorial.py -k "render_topic or format_card" -v`
Expected: FAIL — `no attribute 'render_topic_list'`

- [ ] **Step 3: Write minimal implementation**

```python
TG_LIMIT = 4096


def _split_by_paragraphs(text: str, limit: int = 4000) -> list[str]:
    """Split long text on blank-line boundaries into chunks under limit."""
    if len(text) <= limit:
        return [text]
    chunks, current = [], ""
    for para in text.split("\n\n"):
        candidate = para if not current else current + "\n\n" + para
        if len(candidate) > limit and current:
            chunks.append(current)
            current = para
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def _fmt_date(published) -> str:
    if published is None:
        return "трендинг на момент сбора, без даты выхода"
    return published.strftime("%d.%m.%Y")


def render_topic_list(topics, hours: int, used_ai: bool) -> str:
    """Compact numbered topic list for /digest (Telegram HTML)."""
    lines = [f"📋 <b>Темы к рассмотрению</b> ({hours} ч)", ""]
    if not topics:
        lines.append("Ничего горячего не нашлось. Попробуй расширить окно: /digest 48")
        return "\n".join(lines)
    if not used_ai:
        lines.append("⚠️ Режим без AI — оценки приблизительные (по ключевым словам)")
        lines.append("")
    for i, t in enumerate(topics, 1):
        status = "приоритет" if t["total"] >= PRIORITY_TOTAL else "резерв"
        one_liner = t["one_liner"] and t["one_liner"] or t["article"]["title"]
        lines.append(f"<b>{i}. [{t['total']}/12 · {html.escape(t['rubric'])} · {status}]</b> "
                     f"{html.escape(one_liner)}")
        lines.append(f"   ↳ {html.escape(t['article']['source'])} · /card {i}")
        lines.append("")
    lines.append("Полная карточка: /card 1 … /card %d" % len(topics))
    return "\n".join(lines)


def format_card_messages(card: dict) -> list[str]:
    """Editor card as Telegram HTML messages: the card itself plus the draft
    as a SEPARATE final message (copy-ready). Everything html-escaped."""
    total = card.get("total", 0)
    status = "приоритет" if total >= PRIORITY_TOTAL else \
        ("резерв" if total >= LIST_TOTAL else "ниже порога")
    facts = "\n".join(f"• {html.escape(f)}" for f in card.get("confirmed", []))
    if not facts:
        facts = "—"
    needs = "\n".join(f"• {html.escape(n)}" for n in card.get("needs_check", []))
    if not needs:
        needs = "—"
    sources = "\n".join(
        f'<a href="{html.escape(s["url"], quote=True)}">{html.escape(s["title"])}</a>'
        for s in card.get("sources", [])[:3]) or "—"
    variants = "\n".join(f"{i}. {html.escape(v)}"
                         for i, v in enumerate(card.get("headline_variants", []), 1))
    variants = variants or "—"
    card_text = "\n".join([
        f"🗂 <b>Карточка: {html.escape(card['topic_title'])}</b>",
        f"Рубрика: {html.escape(card['rubric'])} · Оценка: {total}/12 ({status}) · "
        f"формат {html.escape(card.get('post_format') or '—')}",
        "Дата публикации: " + _fmt_date(card.get("published")),
        "",
        "Краткое содержание:",
        html.escape(card["summary"]),
        "",
    ])
    details = "\n".join(filter(None, [
        f"Почему интересно: {html.escape(card.get('why_interesting', ''))}" if card.get("why_interesting") else "",
        f"Для кого полезно: {html.escape(card.get('for_whom', ''))}" if card.get("for_whom") else "",
        f"Первый источник: <a href=\"{html.escape(card.get('first_source_url', ''), quote=True)}\">{html.escape(card.get('first_source_url', ''))}</a>",
        f"Доп. источники: {sources}",
        f"Подтверждённые факты: {facts}",
        f"Что требует проверки: {needs}",
        f"Ограничения и риски: {html.escape(card.get('risks', ''))}" if card.get("risks") else "",
        f"Варианты заголовка: {variants}",
        f"Визуальная идея: {html.escape(card.get('visual_idea', ''))}" if card.get("visual_idea") else "",
        "Статус: черновик — на утверждении (кстати, решение принимаешь ты; парсер статусы не пишет)",
    ]))
    msgs = []
    draft_text = "\n".join(filter(None, [
        "✍️ <b>Черновик</b> (формат %s)" % html.escape(card.get("post_format") or "—"),
        html.escape(card["draft_headline"]) if card.get("draft_headline") else "",
        html.escape(card["draft_body"]),
        html.escape(card["draft_cta"]) if card.get("draft_cta") else "",
    ]))
    for chunk in _split_by_paragraphs(card_text + details) + _split_by_paragraphs(draft_text):
        msgs.append(chunk)
    return msgs
```

(«кстати, решение принимаешь ты…» — убрать из реализации, это попало сюда
из черновика спеки; оставить только «Статус: черновик — на утверждении».)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_editorial.py -v`
Expected: все PASS

- [ ] **Step 5: Commit**

```bash
git add editorial.py tests/test_editorial.py
git commit -m "$(cat <<'EOF'
Editorial: topic list and card rendering with 4096 split + HTML escaping

Co-Authored-By: Claude Code <noreply@anthropic.com>
EOF
)"
```

---

### Task 8: Команды бота `/digest`, `/card` + ботовые хелперы

**Files:**
- Modify: `bot.py` (импорты, 2 хендлера, `main()`, `cmd_start`, `post_init`)
- Test: `tests/test_editorial.py` (добавить — хелперы чистые, асинхронные хендлеры не тестируем, как в существующих тестах бота)

**Interfaces:**
- Consumes: `collect_topics`, `select_topics`, `build_card`, `fact_check_card`, `render_topic_list`, `format_card_messages`; из `notifier` — `clean_text`; `news_pipeline.normalize_url`; `editorial.fact_check_card` нужен client — берётся из `bot._groq_client()`.
- Produces (ботовые команды, только рантайм): `/digest [часы]`, `/card <№>`; карточку кэширует `get_or_build_card` в editorial; хелперы для теста: `resolve_topic(topics, n) -> dict | None`, `parse_int_arg(args: list[str]) -> int | None`.

- [ ] **Step 1: Write the failing test**

```python
# --- ботовые хелперы ---

def test_parse_int_arg():
    assert editorial.parse_int_arg(["3"]) == 3
    assert editorial.parse_int_arg([]) is None
    assert editorial.parse_int_arg(["abc"]) is None
    assert editorial.parse_int_arg(["0"]) is None  # 0 не валиден как номер


def test_resolve_topic_bounds():
    topics = [_topic()]
    assert editorial.resolve_topic(topics, 1) is topics[0]
    assert editorial.resolve_topic(topics, 0) is None
    assert editorial.resolve_topic(topics, 2) is None
    assert editorial.resolve_topic([], 1) is None


def test_get_or_build_card_reuses_builtin():
    topic = _topic()
    client = _fake_client(_CARD_RAW)
    first = editorial.get_or_build_card(topic, client)
    raw_sentinel = editorial.build_card(topic, client)
    create_calls = client.chat.completions.create.call_count if hasattr(client.chat.completions.create, "call_count") else None

    # повторный вызов возвращает сохранённую карточку, второй build не выполнялся
    second = editorial.get_or_build_card(topic, None)
    assert second is first
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_editorial.py -k "parse_int or resolve_topic or get_or_build" -v`
Expected: FAIL — `no attribute 'parse_int_arg'`

- [ ] **Step 3: Write minimal implementation**

В `editorial.py`:

```python
def parse_int_arg(args: list[str]) -> int | None:
    """First arg as positive int or None ('/card 3' → 3)."""
    if not args:
        return None
    try:
        n = int(args[0])
    except (TypeError, ValueError):
        return None
    return n if n >= 1 else None


def resolve_topic(topics: list[dict], n: int) -> dict | None:
    """1-based topic lookup or None (out of range)."""
    return topics[n - 1] if 1 <= n <= len(topics) else None


def get_or_build_card(topic: dict, groq_client=None) -> dict:
    """Caching wrapper: once a card is built for a topic it is reused —
    /card again never burns a second AI call on the same topic."""
    if topic.get("card") is None:
        card = build_card(topic, groq_client)
        article = topic["article"]
        card = fact_check_card(card, article, groq_client)
        card["published"] = article.get("published")
        topic["card"] = card
    return topic["card"]


def filter_unseen(topics: list[dict], seen_urls: set[str]) -> list[dict]:
    """Drop topics whose article URL was already carded this session."""
    return [t for t in topics
            if news_pipeline.normalize_url(t["article"]["url"]) not in seen_urls]
```

В `bot.py`:

- импорт: `import editorial` (после `import news_pipeline`).
- хелпер клиента уже есть (`_groq_client`).

Хендлеры (добавить перед `cmd_start` или после `cmd_reply` — в любом месте до `main()`):

```python
async def cmd_digest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    hours = 24
    if context.args:
        try:
            hours = min(max(int(context.args[0]), 2), 168)
        except ValueError:
            pass
    status = await update.message.reply_text(f"Собираю темы за {hours} ч… "
                                             "это пара минут: сбор + полный текст + скоринг")
    client = _groq_client()
    topics, used_ai = editorial.select_topics(editorial.collect_topics(hours), client)

    seen = set(context.bot_data.get("editorial_seen_cards", ()))
    fresh = editorial.filter_unseen(topics, seen)
    context.bot_data["editorial_topics"] = topics
    context.bot_data["editorial_seen_cards"] = seen

    await status.edit_text(editorial.render_topic_list(fresh, hours, used_ai),
                           parse_mode="HTML", disable_web_page_preview=True)


async def cmd_card(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    topics = context.bot_data.get("editorial_topics", [])
    n = editorial.parse_int_arg(context.args)
    if topics and n is None:
        await update.message.reply_text("Номер темы: /card 1 … /card %d" % len(topics))
        return
    if not topics:
        await update.message.reply_text("Сначала /digest — соберёт список тем для карточек.")
        return
    topic = editorial.resolve_topic(topics, n) if n else None
    if topic is None:
        await update.message.reply_text(
            "Нет такой темы. Сначала /digest, потом /card 1 … /card %d" % len(topics))
        return

    status = await update.message.reply_text("Строю карточку: полный текст, факты, черновик…")
    card = editorial.get_or_build_card(topic, _groq_client())
    seen = set(context.bot_data.get("editorial_seen_cards", ()))
    seen.add(news_pipeline.normalize_url(topic["article"]["url"]))
    context.bot_data["editorial_seen_cards"] = seen
    await status.delete()
    for msg in editorial.format_card_messages(card):
        await update.message.reply_html(msg, disable_web_page_preview=True)
```

В `main()`: после `app.add_handler(CommandHandler("reply", cmd_reply))`:

```python
    app.add_handler(CommandHandler("digest", cmd_digest))
    app.add_handler(CommandHandler("card", cmd_card))
```

В `post_init` в `set_my_commands` добавить в топ списка:

```python
        BotCommand("digest", "🗂 Темы для карточек по редакторской инструкции"),
        BotCommand("card", "📄 Полная карточка темы: /card 1"),
```

В `cmd_start` текст — после блока «Новости:» добавить строку:

```
        "/digest — темы для карточек по редакторской инструкции, /card N — полная карточка темы\n"
```

- [ ] **Step 4: Run tests and import sanity**

Run: `python3 -m pytest tests/ -v && python3 -c "import bot"`
Expected: все тесты PASS; импорт без traceback

- [ ] **Step 5: Smoke-check without Telegram**

```bash
python3 - <<'EOF'
import editorial
topics = editorial.collect_topics(48)
print(f"{len(topics)} кандидатов")
topics, used_ai = editorial.select_topics(topics[:3], None)
for i, t in enumerate(topics, 1):
    print(i, t["total"], t["rubric"], t["article"]["title"][:50])
print(editorial.render_topic_list(topics, 48, used_ai)[:500])
EOF
```
Expected: сборка работает, список печатается (без AI ключей — режим фолбэка).

- [ ] **Step 6: Commit**

```bash
git add bot.py editorial.py tests/test_editorial.py
git commit -m "$(cat <<'EOF'
Bot: /digest topic list and /card editor cards

Co-Authored-By: Claude Code <noreply@anthropic.com>
EOF
)"
```

---

### Task 9: Документация + полный прогон

**Files:**
- Modify: `CLAUDE.md`, `README.md`
- Test: весь набор

**Interfaces:**
- Consumes: всё предыдущее.
- Produces: документация, соответствующая реальности.

- [ ] **Step 1: Документация**

`CLAUDE.md`:
- В раздел «Архитектура» добавить `editorial.py` в список потребителей `news_pipeline.py`.
- Новую секцию «Карточки для редактора (`editorial.py`)»: конвейер `/digest`/`/card`, источники (4 RSS-блога + HN + GitHub Trending + Product Hunt + GitHub Blog + HF trending; **не входят**: Zerocoder, Google News, ZDNet, Simon Willison, Reddit, Telegram-каналы, arXiv), скоринг 6 критериев 0–12 (пороги 9/6), рубрики и форматы, карточка §8, фактчекинг черновика поверх механики notifier, фолбэк без Groq, хранение в `bot_data` до перезапуска, ссыку на спеку.
- В раздел про бот-команды упомянуть `/digest` и `/card` («отбор по редакторской инструкции, не автоотправка — рубильник их не гейтит»).

`README.md`: обновить описание бота (добавить `/digest`, `/card`) и структуру файлов (добавить `editorial.py`, `docs/`).

- [ ] **Step 2: Полный прогон**

Run: `python3 -m pytest tests/ -v`
Expected: все тесты всех файлов PASS, без warnings в выводе pytest (только статистика).

- [ ] **Step 3: Импорт-санити всех точек входа**

Run:
```bash
python3 -c "import monitor; import notifier; import bot; import editorial; import news_pipeline; print('imports ok')"
```
Expected: `imports ok`

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md README.md
git commit -m "$(cat <<'EOF'
Docs: editorial cards (/digest, /card) in CLAUDE.md and README

Co-Authored-By: Claude Code <noreply@anthropic.com>
EOF
)"
```

---

## Self-review notes (already applied)

- Спека покрыта: источники (T1, T2), сбор (T2), скоринг (T3), отбор (T4), карточка (T5), фактчекинг (T6), форматирование/UX (T7), бот-команды (T8), документация (T9). Вне скоупа спеки (статистика Zerocoder, arXiv, allowlist, monitor) задач нет — осознанно.
- Сигнатуры согласованы между задачами: `select_topics` → topic dict → `build_card(topic, client)` → card dict → `fact_check_card(card, article, client)` → `format_card_messages(card)`; `get_or_build_card` кэширует по `topic["card"]`.
- Открытая деталь реализации, зафиксированная в плане: парсер фактчекера карточки отдельный (`_parse_card_factcheck`), вердикты внутри — через `notifier._parse_factcheck_json` (переиспользование схемы вердиктов, как в спеке).
- `_split_by_paragraphs` без дегенеративных кейсов: если один абзац длиннее лимита — он попадёт в чанк один и может превысить 4096; принимается (Telegram-лимит почти не достигается абзацем, а тест ловит регрессию на реальной длине).