# news_pipeline.py Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a shared `news_pipeline.py` module that gives `monitor.py`, `notifier.py`, and `bot.py` full-text-aware structured AI scoring, cross-source and semantic deduplication, and three new keyless sources — then wire it into all three scripts, switch `bot.py`'s overview commands to a short-description-first flow, and change the ready-to-post format from 3 headlines/3 CTAs to 1+1.

**Architecture:** One new module, `news_pipeline.py`, holds everything reusable (fetchers, full-text extraction, structured scoring, dedup). `monitor.py` and `notifier.py` keep their own `KEYWORDS`/`HIGH_VALUE`/`FEEDS` (per existing CLAUDE.md convention) and import `news_pipeline` for everything else. `bot.py` imports from all three.

**Tech Stack:** Python 3, `groq`, `feedparser`, `trafilatura` (new), `pydantic` (new), `tenacity` (new), `pytest` (new, dev-only), stdlib `urllib`/`json`/`re`.

## Global Constraints

- Only keyless APIs — no new paid services or API keys (spec: "Ограничения").
- `KEYWORDS`/`HIGH_VALUE` stay duplicated in `monitor.py` and `notifier.py`; all other new logic lives in `news_pipeline.py` (spec: "Решение: общий модуль").
- Full-text extraction (`extract_full_text`) is only called for articles that already passed the keyword filter (spec: "Full-text экстракция + структурированный AI-скоринг").
- AI scoring and both dedup steps must fail gracefully back to keyword-only behavior — never raise out of the pipeline (spec: "Отказоустойчивость").
- AI calls are sequential, never parallel, to stay inside Groq's free-tier rate limit (spec: "Стоимость").
- No scheduling: `monitor.py`/`notifier.py` stay manually/cron-invokable but nothing is scheduled; all new behavior is reachable only through `bot.py` on-demand commands (spec: "Команды бота").
- Ready-to-post format is **1 headline + 1 CTA** everywhere `generate_post()` is used — this supersedes the old 3+3 format (spec: "Формат готового поста").
- New `HIGH_VALUE`/`KEYWORDS` terms (double-weighted): `skill`, `скил`, `скилы`, `agent skills`, `prompt`, `промпт`, `промты`, `mcp`, `lifehack`, `лайфхак`, `лайфхаки` (spec: "Новые ключевые слова").

---

## File Structure

- **Create** `news_pipeline.py` — fetchers (Hacker News, GitHub Trending, Google News), `extract_full_text`, `AnalysisResult` + `score_article_ai`, `dedup_cross_source`, `dedup_semantic`.
- **Create** `conftest.py` (repo root, empty) — lets `pytest` resolve `import news_pipeline` etc. from `tests/`.
- **Create** `tests/test_news_pipeline.py` — unit tests for every pure function in `news_pipeline.py`.
- **Create** `tests/test_notifier.py` — unit test for the new single-marker `_parse_sections` behavior.
- **Create** `tests/test_bot.py` — unit test for `bot.py`'s new `_format_brief`.
- **Modify** `requirements.txt` — add `trafilatura`, `pydantic`, `tenacity`, `pytest`.
- **Modify** `monitor.py` — new `KEYWORDS`/`HIGH_VALUE` terms, `GOOGLE_NEWS_QUERIES`, wire new sources + dedup into `main()`, rewrite `analyze_with_ai()` to use `score_article_ai`.
- **Modify** `notifier.py` — new `KEYWORDS`/`HIGH_VALUE` terms, `GOOGLE_NEWS_QUERIES`, wire new sources + dedup + AI scoring into `main()`, change `generate_post()` to 1 headline + 1 CTA.
- **Modify** `bot.py` — `_send_posts()` and `cmd_reddit()` switch to short-description mode via new `_format_brief()`, update `cmd_start()` help text and `post_init()` command descriptions.
- **Modify** `CLAUDE.md` — document `news_pipeline.py`, new sources, new keywords, new post format, curl-deploy note.

---

## Task 1: Dependencies and test scaffolding

**Files:**
- Modify: `requirements.txt`
- Create: `conftest.py`

**Interfaces:**
- Produces: a working `pytest tests/ -v` command usable by every later task.

- [ ] **Step 1: Add new dependencies**

Edit `requirements.txt` to:

```
feedparser>=6.0.10
groq>=1.0.0
python-dotenv>=1.0.0
rich>=13.0.0
python-telegram-bot>=20.0
trafilatura>=1.8.0
pydantic>=2.0.0
tenacity>=8.2.0
pytest>=7.4.0
```

- [ ] **Step 2: Install dependencies**

Run: `pip install -r requirements.txt`
Expected: all packages install without errors.

- [ ] **Step 3: Create root conftest.py so pytest can import repo-root modules**

Create `conftest.py`:

```python
# Empty on purpose: its presence makes pytest add the repo root to
# sys.path in "prepend" import mode, so tests can `import news_pipeline`,
# `import monitor`, etc. without a src layout or installed package.
```

- [ ] **Step 4: Verify imports work**

Run: `python3 -c "import trafilatura, pydantic, tenacity, pytest; print('ok')"`
Expected: prints `ok`.

- [ ] **Step 5: Commit**

```bash
git add requirements.txt conftest.py
git commit -m "Add trafilatura/pydantic/tenacity/pytest dependencies"
```

---

## Task 2: news_pipeline.py — URL normalization and cross-source dedup

**Files:**
- Create: `news_pipeline.py`
- Create: `tests/test_news_pipeline.py`

**Interfaces:**
- Produces: `normalize_url(url: str) -> str`, `dedup_cross_source(articles: list[dict]) -> list[dict]`.
- Consumes: article dicts shaped `{"title", "url", "summary", "source", "published"}` (the shape already used by `monitor.fetch_articles`/`notifier.fetch_recent`).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_news_pipeline.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_news_pipeline.py -v`
Expected: `ModuleNotFoundError: No module named 'news_pipeline'`

- [ ] **Step 3: Create news_pipeline.py with normalize_url and dedup_cross_source**

Create `news_pipeline.py`:

```python
#!/usr/bin/env python3
"""Shared pipeline helpers for monitor.py, notifier.py, and bot.py: extra
keyless sources, full-text extraction, structured AI scoring, and
deduplication. See docs/superpowers/specs/2026-07-22-news-pipeline-design.md."""

from urllib.parse import urlsplit, parse_qsl, urlencode, urlunsplit

_TRACKING_QUERY_PARAMS = {
    "fbclid", "gclid", "dclid", "igshid", "mc_cid", "mc_eid", "msclkid",
    "ttclid", "twclid", "vero_id", "li_fat_id", "_ga",
}


def normalize_url(url: str) -> str:
    """Normalized identity key for cross-source dedup: lowercase host,
    no 'www.', no trailing slash, no tracking query params, scheme ignored."""
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = parsed.path.rstrip("/") or "/"
    kept_params = sorted(
        (k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_QUERY_PARAMS
    )
    query = urlencode(kept_params)
    return urlunsplit(("https", host, path, query, ""))


def dedup_cross_source(articles: list[dict]) -> list[dict]:
    """Merge articles that point at the same normalized URL. Keeps the
    richer (longer) summary as primary and combines distinct source names
    with ' + '."""
    groups: dict[str, list[dict]] = {}
    for a in articles:
        groups.setdefault(normalize_url(a["url"]), []).append(a)

    merged = []
    for group in groups.values():
        if len(group) == 1:
            merged.append(group[0])
            continue
        primary = dict(max(group, key=lambda a: len(a.get("summary", ""))))
        other_sources = [
            a["source"] for a in group
            if a["source"] != primary["source"]
        ]
        if other_sources:
            names = list(dict.fromkeys([primary["source"], *other_sources]))
            primary["source"] = " + ".join(sorted(names))
        merged.append(primary)
    return merged
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_news_pipeline.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add news_pipeline.py tests/test_news_pipeline.py
git commit -m "Add URL normalization and cross-source dedup to news_pipeline"
```

---

## Task 3: news_pipeline.py — structured AI scoring

**Files:**
- Modify: `news_pipeline.py`
- Modify: `tests/test_news_pipeline.py`

**Interfaces:**
- Consumes: `normalize_url` (Task 2, unused here but same module).
- Produces: `AnalysisResult` (pydantic model with `.score: int`, `.reason: str`, `.summary: str`), `score_article_ai(article: dict, groq_client, full_text: str | None = None) -> AnalysisResult | None`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_news_pipeline.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_news_pipeline.py -v`
Expected: `ImportError: cannot import name 'AnalysisResult'`

- [ ] **Step 3: Add AnalysisResult, parsing, and score_article_ai**

Append to `news_pipeline.py`:

```python
import json
import re

from pydantic import BaseModel, Field, ValidationError
from tenacity import retry, stop_after_attempt, wait_exponential


class AnalysisResult(BaseModel):
    score: int = Field(ge=0, le=5)
    reason: str
    summary: str


def _parse_analysis_json(raw: str) -> AnalysisResult | None:
    """Extract and validate a {score, reason, summary} JSON object from a
    raw model response, tolerating ```json fences or surrounding prose."""
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    try:
        return AnalysisResult.model_validate(data)
    except ValidationError:
        return None


_ANALYSIS_SYSTEM_PROMPT = (
    "Ты пишешь исключительно на русском языке. Никаких китайских, японских "
    "или других иероглифов — только кириллица, латиница в именах собственных и цифры. "
    "Отвечай СТРОГО одним JSON-объектом, без пояснений и markdown-разметки."
)


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
def _call_analysis_model(client, article: dict, full_text: str | None) -> str:
    content = full_text or article["summary"]
    prompt = (
        "Ты — опытный русскоязычный редактор Telegram-канала об ИИ и вайбкодинге.\n\n"
        "Оцени статью по релевантности для аудитории, которой интересны:\n"
        "— новые ИИ-инструменты и нейросети\n"
        "— вайбкодинг и автоматизация с помощью ИИ\n"
        "— практические кейсы применения ИИ, скилы, промты, MCP, лайфхаки\n\n"
        "Ответь строго одним JSON-объектом вида:\n"
        '{"score": <целое 0-5>, "reason": "<почему такая оценка, коротко>", '
        '"summary": "<одно законченное предложение на живом русском языке — суть новости>"}\n\n'
        f"Источник: {article['source']}\n"
        f"Заголовок: {article['title']}\n"
        f"Содержание: {content[:3000]}"
    )
    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[
            {"role": "system", "content": _ANALYSIS_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        max_tokens=400,
    )
    return response.choices[0].message.content


def score_article_ai(article: dict, groq_client, full_text: str | None = None) -> AnalysisResult | None:
    """Score one article with structured AI output (0-5 + reason + one-line
    summary). Returns None if the call or parsing failed after retries —
    callers must fall back to keyword scoring."""
    try:
        raw = _call_analysis_model(groq_client, article, full_text)
    except Exception:
        return None
    return _parse_analysis_json(raw)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_news_pipeline.py -v`
Expected: all passed (10 total so far).

- [ ] **Step 5: Manually verify a live call**

Run (requires `GROQ_API_KEY` in `.env`):

```bash
python3 -c "
from dotenv import load_dotenv; load_dotenv()
import os
from groq import Groq
from news_pipeline import score_article_ai

client = Groq(api_key=os.getenv('GROQ_API_KEY'))
article = {'title': 'OpenAI выпустила GPT-5.1', 'summary': 'OpenAI объявила о выходе новой модели GPT-5.1 с улучшенным качеством кода.', 'url': 'https://example.com', 'source': 'Test'}
result = score_article_ai(article, client)
print(result)
"
```

Expected: prints an `AnalysisResult(score=..., reason=..., summary=...)` with a plausible score and a Russian one-sentence summary.

- [ ] **Step 6: Commit**

```bash
git add news_pipeline.py tests/test_news_pipeline.py
git commit -m "Add structured AI scoring (AnalysisResult, score_article_ai) to news_pipeline"
```

---

## Task 4: news_pipeline.py — full-text extraction

**Files:**
- Modify: `news_pipeline.py`
- Modify: `tests/test_news_pipeline.py`

**Interfaces:**
- Produces: `extract_full_text(url: str) -> str | None`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_news_pipeline.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_news_pipeline.py -v`
Expected: `ImportError: cannot import name '_extract_from_html'`

- [ ] **Step 3: Add extract_full_text and _extract_from_html**

Append to `news_pipeline.py`:

```python
import urllib.request

import trafilatura

_BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"


def _extract_from_html(html: str) -> str | None:
    text = trafilatura.extract(html, favor_recall=True)
    if not text or len(text) < 200:
        return None
    return text


def extract_full_text(url: str) -> str | None:
    """Fetch `url` and extract the article body via trafilatura. Returns
    None on any failure (network error, blocked page, too-short
    extraction) — callers must fall back to the RSS summary."""
    req = urllib.request.Request(url, headers={"User-Agent": _BROWSER_UA})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
    except Exception:
        return None
    return _extract_from_html(html)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_news_pipeline.py -v`
Expected: all passed (12 total so far).

- [ ] **Step 5: Manually verify against a real URL**

Run:

```bash
python3 -c "
from news_pipeline import extract_full_text
text = extract_full_text('https://ya.zerocoder.ru/')
print(len(text) if text else None)
print((text or '')[:300])
"
```

Expected: prints a length > 200 and a readable Russian text excerpt (not None — if the site blocks the request, try a different real article URL to confirm the function itself works).

- [ ] **Step 6: Commit**

```bash
git add news_pipeline.py tests/test_news_pipeline.py
git commit -m "Add full-text extraction (extract_full_text) to news_pipeline"
```

---

## Task 5: news_pipeline.py — semantic dedup

**Files:**
- Modify: `news_pipeline.py`
- Modify: `tests/test_news_pipeline.py`

**Interfaces:**
- Consumes: articles with a `"score"` key (set by `score_article_ai` callers before this runs).
- Produces: `dedup_semantic(articles: list[dict], groq_client) -> list[dict]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_news_pipeline.py`:

```python
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


def test_apply_duplicate_groups_keeps_highest_score():
    articles = [
        {"title": "A", "score": 3},
        {"title": "B", "score": 5},
        {"title": "C", "score": 1},
    ]
    result = _apply_duplicate_groups(articles, [[0, 1]])
    assert result == [{"title": "B", "score": 5}, {"title": "C", "score": 1}]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_news_pipeline.py -v`
Expected: `ImportError: cannot import name '_parse_duplicate_groups'`

- [ ] **Step 3: Add the semantic dedup functions**

Append to `news_pipeline.py`:

```python
def _parse_duplicate_groups(raw: str, n_items: int) -> list[list[int]]:
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    groups = data.get("duplicates", [])
    if not isinstance(groups, list):
        return []
    valid = []
    for group in groups:
        if not isinstance(group, list) or len(group) < 2:
            continue
        if all(isinstance(i, int) and 0 <= i < n_items for i in group):
            valid.append(group)
    return valid


def _apply_duplicate_groups(articles: list[dict], groups: list[list[int]]) -> list[dict]:
    drop: set[int] = set()
    for group in groups:
        ranked = sorted(group, key=lambda i: articles[i].get("score", 0), reverse=True)
        drop.update(ranked[1:])
    return [a for i, a in enumerate(articles) if i not in drop]


def dedup_semantic(articles: list[dict], groq_client) -> list[dict]:
    """Collapse near-duplicate stories (same event, different sources)
    using one batched AI call over already-scored articles. Falls back to
    `articles` unchanged if the call or parsing fails."""
    if len(articles) <= 1:
        return articles

    numbered = "\n\n".join(
        f"[{i}] {a['title']}\n{a.get('verdict') or a.get('summary', '')[:200]}"
        for i, a in enumerate(articles)
    )
    prompt = (
        "Ниже пронумерованный список новостей. Найди группы, где несколько "
        "пунктов рассказывают об одном и том же событии (просто с разных сайтов). "
        "Ответь строго одним JSON-объектом:\n"
        '{"duplicates": [[i, j, ...], ...]}\n'
        "Каждая группа — индексы (начиная с 0) новостей об одном и том же событии, "
        'минимум 2 индекса. Если дублей нет — верни {"duplicates": []}.\n\n'
        f"{numbered}"
    )
    try:
        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": "Отвечай СТРОГО одним JSON-объектом, без пояснений."},
                {"role": "user", "content": prompt},
            ],
            max_tokens=500,
        )
        raw = response.choices[0].message.content
    except Exception:
        return articles

    groups = _parse_duplicate_groups(raw, len(articles))
    if not groups:
        return articles
    return _apply_duplicate_groups(articles, groups)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_news_pipeline.py -v`
Expected: all passed (17 total so far).

- [ ] **Step 5: Commit**

```bash
git add news_pipeline.py tests/test_news_pipeline.py
git commit -m "Add semantic dedup (dedup_semantic) to news_pipeline"
```

---

## Task 6: news_pipeline.py — new keyless sources

**Files:**
- Modify: `news_pipeline.py`
- Modify: `tests/test_news_pipeline.py`

**Interfaces:**
- Produces: `fetch_hackernews(hours: int = 24, min_score: int = 100, fetch_top: int = 30) -> list[dict]`, `fetch_github_trending(hours: int = 24, languages: list[str] | None = None, min_stars: int = 5) -> list[dict]`, `fetch_google_news(queries: list[str], hours: int = 24) -> list[dict]`. All return the same article shape as `dedup_cross_source` consumes.

- [ ] **Step 1: Write the failing tests (pure helpers only — the fetchers themselves hit live APIs and are verified manually in Step 5)**

Append to `tests/test_news_pipeline.py`:

```python
from news_pipeline import _hn_story_to_article, _github_trending_period, _google_news_time_operator


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_news_pipeline.py -v`
Expected: `ImportError: cannot import name '_hn_story_to_article'`

- [ ] **Step 3: Add the fetchers and their pure helpers**

Append to `news_pipeline.py`:

```python
import urllib.parse

import feedparser
from datetime import datetime, timedelta, timezone


def _safe_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _hn_story_to_article(story: dict) -> dict | None:
    title = story.get("title")
    if not title:
        return None
    story_id = story["id"]
    url = story.get("url") or f"https://news.ycombinator.com/item?id={story_id}"
    return {
        "title": title,
        "url": url,
        "summary": (story.get("text") or "")[:600],
        "source": "Hacker News",
        "published": None,
    }


def fetch_hackernews(hours: int = 24, min_score: int = 100, fetch_top: int = 30) -> list[dict]:
    """Top Hacker News stories via the keyless Firebase API, filtered by
    minimum score and publish time. Returns [] on any failure."""
    base = "https://hacker-news.firebaseio.com/v0"
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    try:
        with urllib.request.urlopen(f"{base}/topstories.json", timeout=10) as resp:
            story_ids = json.loads(resp.read())[:fetch_top]
    except Exception:
        return []

    articles = []
    for story_id in story_ids:
        try:
            with urllib.request.urlopen(f"{base}/item/{story_id}.json", timeout=10) as resp:
                story = json.loads(resp.read())
        except Exception:
            continue
        if not story or story.get("score", 0) < min_score:
            continue
        story_time = story.get("time")
        if story_time and datetime.fromtimestamp(story_time, tz=timezone.utc) < cutoff:
            continue
        article = _hn_story_to_article(story)
        if article:
            articles.append(article)
    return articles


def _github_trending_period(hours: int) -> str:
    return "past_24_hours" if hours <= 24 else "past_28_days"


def fetch_github_trending(hours: int = 24, languages: list[str] | None = None, min_stars: int = 5) -> list[dict]:
    """Trending GitHub repos via the keyless OSS Insight API. Returns []
    entries for languages that fail; never raises."""
    languages = languages or ["Python", "TypeScript", "All"]
    period = _github_trending_period(hours)
    articles = []
    for lang in languages:
        params = urllib.parse.urlencode({"period": period, "language": lang})
        req = urllib.request.Request(
            f"https://api.ossinsight.io/v1/trends/repos?{params}",
            headers={"Accept": "application/json", "User-Agent": "Zerocoder-News-Bot/1.0"},
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                payload = json.loads(resp.read())
        except Exception:
            continue
        rows = (payload.get("data") or {}).get("rows") or []
        for row in rows:
            repo = row.get("repo_name")
            stars = _safe_int(row.get("stars"))
            if not repo or stars < min_stars:
                continue
            description = (row.get("description") or "").strip()
            articles.append({
                "title": f"{repo} (+{stars}⭐)",
                "url": f"https://github.com/{repo}",
                "summary": description[:600],
                "source": "GitHub Trending",
                "published": None,
            })
    return articles


def _google_news_time_operator(hours: int) -> str:
    if hours <= 100:
        return f"when:{hours}h"
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    return f"after:{since.strftime('%Y-%m-%d')}"


def fetch_google_news(queries: list[str], hours: int = 24) -> list[dict]:
    """Google News RSS search for each query in `queries`. Keyless. Skips
    queries that fail; never raises."""
    operator = _google_news_time_operator(hours)
    articles = []
    for query in queries:
        params = urllib.parse.urlencode({
            "q": f"{query} {operator}",
            "hl": "ru", "gl": "RU", "ceid": "RU:ru",
        })
        try:
            feed = feedparser.parse(f"https://news.google.com/rss/search?{params}")
        except Exception:
            continue
        for entry in feed.entries:
            title = entry.get("title", "")
            link = entry.get("link", "")
            if not title or not link:
                continue
            summary = entry.get("summary", entry.get("description", ""))
            summary = re.sub(r"<[^>]+>", " ", summary)
            articles.append({
                "title": title,
                "url": link,
                "summary": re.sub(r"\s+", " ", summary).strip()[:600],
                "source": f"Google News: {query}",
                "published": None,
            })
    return articles
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_news_pipeline.py -v`
Expected: all passed (24 total so far).

- [ ] **Step 5: Manually verify live fetches**

Run:

```bash
python3 -c "
from news_pipeline import fetch_hackernews, fetch_github_trending, fetch_google_news
hn = fetch_hackernews()
gh = fetch_github_trending()
gn = fetch_google_news(['вайбкодинг', 'Claude Code', 'AI coding agent'])
print('HN:', len(hn), hn[0]['title'] if hn else None)
print('GitHub Trending:', len(gh), gh[0]['title'] if gh else None)
print('Google News:', len(gn), gn[0]['title'] if gn else None)
"
```

Expected: no exceptions; each source prints a count ≥ 0 and, if > 0, a plausible title. Zero results for a source (e.g., no HN story ≥ 100 points in the window) is acceptable, not a failure.

- [ ] **Step 6: Commit**

```bash
git add news_pipeline.py tests/test_news_pipeline.py
git commit -m "Add Hacker News, GitHub Trending, and Google News fetchers to news_pipeline"
```

---

## Task 7: Wire news_pipeline into monitor.py

**Files:**
- Modify: `monitor.py`

**Interfaces:**
- Consumes: `news_pipeline.fetch_hackernews`, `news_pipeline.fetch_github_trending`, `news_pipeline.fetch_google_news`, `news_pipeline.dedup_cross_source`, `news_pipeline.score_article_ai`, `news_pipeline.extract_full_text`, `news_pipeline.dedup_semantic` (all from Tasks 2-6).
- Produces: `GOOGLE_NEWS_QUERIES` (new module-level constant other files will import).

- [ ] **Step 1: Add the import and new keywords**

In `monitor.py`, add near the top (after the existing `from dotenv import load_dotenv`):

```python
import news_pipeline
```

Replace the `KEYWORDS` list:

```python
KEYWORDS = [
    "ИИ", "нейросеть", "нейросети", "no-code", "nocode", "автоматизация",
    "ChatGPT", "Claude", "Midjourney", "Gemini", "GPT", "LLM",
    "AI", "artificial intelligence", "machine learning", "automation",
    "агент", "agent", "workflow",
    "OpenAI", "Anthropic", "Siri", "Apple Intelligence", "Google AI",
    "Microsoft AI", "Llama", "Mistral", "Grok", "xAI",
    "WWDC", "GPT-4", "GPT-5", "Copilot", "neural network",
    "вайбкодинг", "vibe coding", "vibecoding",
    "skill", "скил", "скилы", "agent skills",
    "prompt", "промпт", "промты",
    "mcp",
    "lifehack", "лайфхак", "лайфхаки",
]
```

Replace the `HIGH_VALUE` set:

```python
HIGH_VALUE = {"chatgpt", "claude", "gpt", "llm", "gemini", "midjourney",
              "no-code", "nocode", "нейросеть", "нейросети", "автоматизация", "agent",
              "openai", "anthropic", "llama", "mistral", "grok", "sora", "copilot",
              "вайбкодинг", "vibe coding", "vibecoding",
              "skill", "скил", "скилы", "agent skills",
              "prompt", "промпт", "промты",
              "mcp",
              "lifehack", "лайфхак", "лайфхаки"}
```

Add, right after `HIGH_VALUE`:

```python
GOOGLE_NEWS_QUERIES = ["вайбкодинг", "Claude Code", "AI coding agent"]
```

- [ ] **Step 2: Rewrite analyze_with_ai to use structured per-article scoring**

Replace the whole `analyze_with_ai` function body:

```python
def analyze_with_ai(articles: list[dict]) -> tuple[list[dict], bool]:
    """Rate articles with structured per-article Groq scoring (full text
    when available); falls back to keyword scoring per-article on failure."""
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        console.print("[dim]Подсказка: добавь GROQ_API_KEY в .env (бесплатно на console.groq.com)[/dim]")
        return analyze_local(articles), False

    client = Groq(api_key=api_key)
    used_ai = False
    for a in articles:
        full_text = news_pipeline.extract_full_text(a["url"])
        result = news_pipeline.score_article_ai(a, client, full_text=full_text)
        if result is None:
            a["score"] = score_article(a)
            a["verdict"] = a["title"]
        else:
            a["score"] = result.score
            a["verdict"] = result.summary
            a["reason"] = result.reason
            used_ai = True

    articles = news_pipeline.dedup_semantic(articles, client)
    return articles, used_ai
```

Delete the old batch-prompt implementation (the `numbered = ...`, `prompt = f"""..."""`, and the manual `[N] SCORE | SUMMARY` line-parsing loop) — it is fully replaced by the per-article loop above.

- [ ] **Step 3: Fetch the new sources in main()**

In `main()`, replace:

```python
    console.print("\n[bold]1. Загружаю RSS-ленты...[/bold]")
    all_articles = fetch_articles(hours)
    console.print(f"\nВсего найдено: [bold]{len(all_articles)}[/bold] статей")
```

with:

```python
    console.print("\n[bold]1. Загружаю RSS-ленты и другие источники...[/bold]")
    all_articles = fetch_articles(hours)
    all_articles += news_pipeline.fetch_hackernews(hours=hours)
    all_articles += news_pipeline.fetch_github_trending(hours=hours)
    all_articles += news_pipeline.fetch_google_news(GOOGLE_NEWS_QUERIES, hours=hours)
    all_articles = news_pipeline.dedup_cross_source(all_articles)
    console.print(f"\nВсего найдено (после дедупа): [bold]{len(all_articles)}[/bold] статей")
```

- [ ] **Step 4: Manually verify**

Run: `python3 monitor.py 48`
Expected: script runs to completion without exceptions, console shows fetch counts for RSS + the three new sources (folded into the total), a relevant-articles count, and a scored digest where each article has a `verdict` (AI summary) and `score`. If `GROQ_API_KEY` is set, confirm at least one article shows a plausible AI-written verdict rather than its raw title.

- [ ] **Step 5: Commit**

```bash
git add monitor.py
git commit -m "Wire news_pipeline sources, dedup, and structured scoring into monitor.py"
```

---

## Task 8: Wire news_pipeline into notifier.py, change post format to 1+1

**Files:**
- Modify: `notifier.py`
- Create: `tests/test_notifier.py`

**Interfaces:**
- Consumes: same `news_pipeline` functions as Task 7, plus `GOOGLE_NEWS_QUERIES` (duplicated here per the KEYWORDS convention, not imported from `monitor.py`, to keep `notifier.py` independently curl-deployable).
- Produces: `generate_post()` unchanged signature, new output format.

- [ ] **Step 1: Add the import, new keywords, and GOOGLE_NEWS_QUERIES**

In `notifier.py`, add near the top:

```python
import news_pipeline
```

Apply the same `KEYWORDS`/`HIGH_VALUE` additions as Task 7 Step 1 (identical lists — this file keeps its own copy per CLAUDE.md's duplication convention). Add after `HIGH_VALUE`:

```python
GOOGLE_NEWS_QUERIES = ["вайбкодинг", "Claude Code", "AI coding agent"]
```

- [ ] **Step 2: Write the failing test for the new post-format marker parsing**

Create `tests/test_notifier.py`:

```python
from notifier import _parse_sections, _SECTION_MARKERS


def test_section_markers_are_single_not_numbered():
    assert _SECTION_MARKERS == ["ЗАГОЛОВОК", "ТЕЛО", "CTA"]


def test_parse_sections_single_markers():
    raw = "ЗАГОЛОВОК: Claude научился писать код лучше 🚀\nТЕЛО: Первый абзац.\n\nВторой абзац.\nCTA: Пробовали уже?"
    sections = _parse_sections(raw)
    assert sections["ЗАГОЛОВОК"] == "Claude научился писать код лучше 🚀"
    assert sections["ТЕЛО"] == "Первый абзац.\n\nВторой абзац."
    assert sections["CTA"] == "Пробовали уже?"
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/test_notifier.py -v`
Expected: `AssertionError` on `test_section_markers_are_single_not_numbered` (current value is the 7-marker list).

- [ ] **Step 4: Change _SECTION_MARKERS and generate_post to 1 headline + 1 CTA**

Replace:

```python
_SECTION_MARKERS = ["ЗАГОЛОВОК1", "ЗАГОЛОВОК2", "ЗАГОЛОВОК3", "ТЕЛО", "CTA1", "CTA2", "CTA3"]
```

with:

```python
_SECTION_MARKERS = ["ЗАГОЛОВОК", "ТЕЛО", "CTA"]
```

Replace the whole `generate_post` function body (keep the signature and the `fallback` computation unchanged) from `cta_labels = _engagement_labels(3)` through the end of the `try` block:

```python
    cta_label = _engagement_labels(1)[0]

    prompt = (
        "Ты — редактор Telegram-канала Zerocoder об ИИ и вайбкодинге.\n\n"
        "Напиши пост для канала на основе статьи ниже.\n\n"
        f"{_CONTENT_FORMULA}\n\n"
        f"{_NATURAL_TEXT_RULES}\n\n"
        "Ответ дай СТРОГО в этом формате, с этими маркерами в начале строки, ничего от себя не добавляй:\n\n"
        "ЗАГОЛОВОК: <заголовок — до 10 слов, конкретный неожиданный результат или эффект открытия "
        "(«так можно было?!»), эмодзи в конце (выбери из: 💡 🚀 🔍 💻 📊 ⚡ 🛠 🌐 🎯 👀 🤯 — не используй 🤖 и 🧠)>\n"
        "ТЕЛО: <3-4 коротких абзаца (1-3 строки каждый), разделённых пустой строкой:\n"
        "  - кто и что сделал, конкретно (имена, компании, инструменты) — обязателен факт, не только мнение\n"
        "  - если в статье есть проблема и то, как её решили — расскажи по шагам, обычными словами\n"
        "  - что это значит для человека, который занимается ИИ или вайбкодингом — конкретный практический вывод\n"
        "  - можно закончить лёгкой деталью или цитатой из статьи с эмодзи, если это к месту>\n"
        f"CTA: <концовка в духе «{cta_label}», одна строка, без нажима>\n\n"
        "НЕ пиши ссылку и не пиши «Читать далее» — она добавится отдельно кодом.\n\n"
        f"Источник: {article['source']}\n"
        f"Заголовок статьи: {article['title']}\n"
        f"Содержание: {article['summary']}"
    )

    try:
        client = Groq(api_key=api_key)
        response = client.chat.completions.create(
            model="meta-llama/llama-4-scout-17b-16e-instruct",
            messages=[
                {"role": "system", "content": "Пиши исключительно на русском языке. Никогда не смешивай латиницу и кириллицу в одном слове: 'specialistам' — грубая ошибка, пиши 'специалистам'. Латиница допустима только в именах собственных (OpenAI, Anthropic, ChatGPT) и аббревиатурах (AI, IPO). Пиши живым естественным языком, как для друга, без ИИ-штампов и канцелярита — избегай слов «революционный», «трансформационный», «раскрыть потенциал», «оптимизировать», «инновационный», «прорывной»."},
                {"role": "user", "content": prompt},
            ],
            max_tokens=500,
            temperature=0.7,
        )
        raw = clean_text(response.choices[0].message.content)
        sections = _parse_sections(raw)
        headline = sections.get("ЗАГОЛОВОК", "").strip()
        body = sections.get("ТЕЛО", "").strip()
        cta = sections.get("CTA", "").strip()

        if not headline or not cta:
            return fallback

        is_social = article["source"].startswith("X:")
        link_label = "\U0001f4ce Оригинальный пост" if is_social else "\U0001f517 Источник"
        link_line = f'{link_label}: {article["url"]}'

        parts = [f"<b>{headline}</b>", link_line]
        if body:
            parts.append(body)
        parts.append(cta)
        return "\n\n".join(parts)
    except Exception:
        return fallback
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_notifier.py -v`
Expected: 2 passed.

- [ ] **Step 6: Rewrite main() to fetch new sources, dedup, and AI-score before selecting hot articles**

Replace `main()`:

```python
def main():
    sent_urls = load_sent_urls()

    articles = fetch_recent(hours=24)
    articles += news_pipeline.fetch_hackernews(hours=24)
    articles += news_pipeline.fetch_github_trending(hours=24)
    articles += news_pipeline.fetch_google_news(GOOGLE_NEWS_QUERIES, hours=24)
    articles = news_pipeline.dedup_cross_source(articles)

    seen: set[str] = set()
    unique = []
    for a in articles:
        if a["url"] not in seen and a["url"] not in sent_urls:
            seen.add(a["url"])
            unique.append(a)

    relevant = [
        a for a in unique
        if any(_kw_matches(kw, (a["title"] + " " + a["summary"]).lower()) for kw in KEYWORDS)
    ]
    # Cap the expensive full-text+AI pass to the top keyword-scored candidates.
    candidates = sorted(relevant, key=score_article, reverse=True)[:15]

    api_key = os.getenv("GROQ_API_KEY")
    if api_key:
        client = Groq(api_key=api_key)
        for a in candidates:
            full_text = news_pipeline.extract_full_text(a["url"])
            result = news_pipeline.score_article_ai(a, client, full_text=full_text)
            if result:
                a["score"] = result.score
                a["verdict"] = result.summary
            else:
                a["score"] = score_article(a)
        candidates = news_pipeline.dedup_semantic(candidates, client)
    else:
        for a in candidates:
            a["score"] = score_article(a)

    hot = sorted([a for a in candidates if a["score"] >= 3], key=lambda a: a["score"], reverse=True)[:7]

    print(f"Новых за 24ч: {len(unique)}, релевантных: {len(relevant)}, горячих (3+): {len(hot)}")

    for article in hot:
        post = generate_post(article)
        if send_to_telegram(post):
            save_sent_url(article["url"], sent_urls)
            print(f"Отправлено: {article['title']}")

    if not hot:
        print("Новых важных статей нет.")
```

Note the hot threshold changed from keyword `score >= 2` to AI `score >= 3` — matching `monitor.py`'s bar now that scoring is AI-based rather than raw keyword counting.

- [ ] **Step 7: Manually verify generate_post's new format without sending to Telegram**

Run (requires `GROQ_API_KEY`; does **not** call `send_to_telegram`, so nothing is posted):

```bash
python3 -c "
from dotenv import load_dotenv; load_dotenv()
from notifier import generate_post
article = {'title': 'OpenAI выпустила новую модель', 'summary': 'OpenAI объявила о выходе GPT-5.1 с улучшенным качеством генерации кода.', 'url': 'https://example.com', 'source': 'Test'}
print(generate_post(article))
"
```

Expected: output has exactly one bold headline line, one `Источник:` line, a body, and exactly one CTA line — no "Варианты заголовка:"/"Варианты CTA:" wrapper.

- [ ] **Step 8: Commit**

```bash
git add notifier.py tests/test_notifier.py
git commit -m "Wire news_pipeline into notifier.py; change post format to 1 headline + 1 CTA"
```

---

## Task 9: bot.py — short-description mode for overview commands

**Files:**
- Modify: `bot.py`
- Create: `tests/test_bot.py`

**Interfaces:**
- Consumes: `news_pipeline.fetch_hackernews`, `.fetch_github_trending`, `.fetch_google_news`, `.dedup_cross_source`, `.score_article_ai`, `.extract_full_text`, `.dedup_semantic` (Tasks 2-6); `GOOGLE_NEWS_QUERIES` (from `monitor.py`, Task 7).
- Produces: `_format_brief(article: dict) -> str`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_bot.py`:

```python
from bot import _format_brief


def test_format_brief_uses_verdict_when_present():
    article = {"title": "Заголовок статьи", "verdict": "Короткое описание сути новости.",
               "source": "ZDNet", "url": "https://example.com/a"}
    result = _format_brief(article)
    assert "Заголовок статьи" in result
    assert "Короткое описание сути новости." in result
    assert "https://example.com/a" in result


def test_format_brief_falls_back_to_title_without_verdict():
    article = {"title": "Заголовок статьи", "source": "ZDNet", "url": "https://example.com/a"}
    result = _format_brief(article)
    assert result.count("Заголовок статьи") == 2  # bold title + fallback "verdict" line
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_bot.py -v`
Expected: `ImportError: cannot import name '_format_brief'`

- [ ] **Step 3: Add the import, _format_brief, and the candidate cap constant**

In `bot.py`, change the import line:

```python
from monitor import FEEDS, fetch_articles, filter_by_keywords, GOOGLE_NEWS_QUERIES
```

Add near the top (after the other module-level constants):

```python
import news_pipeline

_CANDIDATE_CAP = {2: 10, 24: 15, 168: 20}


def _format_brief(article: dict) -> str:
    verdict = article.get("verdict") or article["title"]
    return (
        f'<b>{article["title"]}</b>\n'
        f'{verdict}\n'
        f'<i>{article["source"]}</i> · <a href="{article["url"]}">Читать →</a>'
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_bot.py -v`
Expected: 2 passed.

- [ ] **Step 5: Rewrite _send_posts to fetch new sources and use short descriptions**

Replace `_send_posts`:

```python
async def _send_posts(update: Update, hours: int) -> None:
    period = {2: "2 часа", 24: "24 часа", 168: "7 дней"}[hours]
    limit = {2: 5, 24: 7, 168: 10}[hours]

    status = await update.message.reply_text(f"Ищу новости за {period}…")

    articles = fetch_articles(hours)
    articles += news_pipeline.fetch_hackernews(hours=hours)
    articles += news_pipeline.fetch_github_trending(hours=hours)
    articles += news_pipeline.fetch_google_news(GOOGLE_NEWS_QUERIES, hours=hours)
    articles = news_pipeline.dedup_cross_source(articles)
    relevant = filter_by_keywords(articles)
    candidates = sorted(relevant, key=score_article, reverse=True)[:_CANDIDATE_CAP[hours]]

    if not candidates:
        await status.edit_text(f"За последние {period} ничего горячего не нашлось.")
        return

    await status.edit_text(f"Оцениваю {len(candidates)} материалов…")
    client = _groq_client()
    if client:
        for a in candidates:
            full_text = news_pipeline.extract_full_text(a["url"])
            result = news_pipeline.score_article_ai(a, client, full_text=full_text)
            if result:
                a["score"] = result.score
                a["verdict"] = result.summary
            else:
                a["score"] = score_article(a)
        candidates = news_pipeline.dedup_semantic(candidates, client)
    else:
        for a in candidates:
            a["score"] = score_article(a)

    hot = sorted([a for a in candidates if a["score"] >= 2], key=lambda a: a["score"], reverse=True)[:limit]

    if not hot:
        await status.edit_text(f"За последние {period} ничего горячего не нашлось.")
        return

    await status.edit_text(f"Нашла {len(hot)} материалов:")
    for article in hot:
        await update.message.reply_html(_format_brief(article), disable_web_page_preview=True)
    await status.delete()
```

- [ ] **Step 6: Rewrite cmd_reddit to use short descriptions**

Replace `cmd_reddit`:

```python
async def cmd_reddit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    status = await update.message.reply_text("Ищу интересные ветки на Reddit… (около 15 секунд — так надо, чтобы Reddit не отдавал 429)")

    threads = fetch_reddit_threads()
    relevant = [t for t in threads if score_article(t) >= 1]
    ranked = sorted(relevant, key=score_article, reverse=True)

    picked: list[dict] = []
    seen_urls: set[str] = set()
    for t in ranked:
        if t["url"] in seen_urls:
            continue
        seen_urls.add(t["url"])
        picked.append(t)
        if len(picked) == 3:
            break

    if not picked:
        await status.edit_text("Не нашла ничего интересного на Reddit прямо сейчас.")
        return

    await status.edit_text(f"Нашла {len(picked)} веток, готовлю описания…")
    client = _groq_client()
    for t in picked:
        if client:
            full_text = news_pipeline.extract_full_text(t["url"])
            result = news_pipeline.score_article_ai(t, client, full_text=full_text)
            if result:
                t["verdict"] = result.summary
        await update.message.reply_html(_format_brief(t), disable_web_page_preview=True)
    await status.delete()
```

- [ ] **Step 7: Update help text and command descriptions**

In `cmd_start`, replace the "Новости:" block:

```python
        "Новости:\n"
        "/news2 — горячее за последние 2 часа, заголовок + короткое описание\n"
        "/news24 — лучшее за сутки, заголовок + короткое описание\n"
        "/news7 — главное за неделю, заголовок + короткое описание\n"
        "/list7 — быстрый список за неделю по каждому источнику отдельно (заголовок + ссылка, без ИИ)\n"
        "/reddit — интересные ветки из AI-сабреддитов, заголовок + короткое описание\n\n"
        "Понравилась новость из обзора — скопируй её ссылку и вызови /post <ссылка>, "
        "получишь готовый пост.\n\n"
```

In `post_init`, update the descriptions:

```python
        BotCommand("news2",   "🔥 Горячее за последние 2 часа (кратко)"),
        BotCommand("news24",  "📰 Лучшее за сутки (кратко)"),
        BotCommand("news7",   "📅 Главное за неделю (кратко)"),
        BotCommand("list7",   "📋 Список новостей за неделю по источникам"),
        BotCommand("reddit",  "👽 Интересные ветки с Reddit (кратко)"),
```

(the `post`, `rewrite`, `reply` entries stay unchanged)

- [ ] **Step 8: Manually verify by running the bot**

Run: `python3 bot.py` (requires `TELEGRAM_BOT_TOKEN`, and `GROQ_API_KEY` for the AI-scored path), then in Telegram send `/news24` and `/reddit` to the bot.
Expected: replies are short (title + one-line description + link), not full 1-headline+CTA posts; picking a link from one of those replies and sending `/post <that link>` still returns a full ready-to-post message.

- [ ] **Step 9: Commit**

```bash
git add bot.py tests/test_bot.py
git commit -m "Switch bot.py overview commands to short-description mode"
```

---

## Task 10: Update CLAUDE.md

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Update "Архитектура" section**

After the existing architecture diagram paragraph, add:

```markdown
Общая инфраструктура (новые источники, полнотекстовая экстракция, структурированный AI-скоринг, дедупликация) вынесена в `news_pipeline.py`, который импортируют `monitor.py`, `notifier.py` и `bot.py`. `KEYWORDS`/`HIGH_VALUE` остаются продублированы в `monitor.py`/`notifier.py` — единственное, что по-прежнему нужно синхронизировать вручную.
```

- [ ] **Step 2: Update "RSS-источники" section**

Rename the heading and add the new sources:

```markdown
## Источники

RSS (10 лент): Zerocoder, ZDNet, Forbes AI, TechCrunch AI, OpenAI Blog, HuggingFace Blog, DeepMind Blog, Karpathy (Substack), One Useful Thing (Ethan Mollick), DAIR AI (Medium).

Дополнительно, через `news_pipeline.py` (все keyless, без API-ключей):
- **Hacker News** — топ-истории с `score ≥ 100` (Firebase API).
- **GitHub Trending** — через `api.ossinsight.io`, языки Python/TypeScript/All, `min_stars ≥ 5`.
- **Google News RSS search** — по запросам из `GOOGLE_NEWS_QUERIES` («вайбкодинг», «Claude Code», «AI coding agent»).
```

- [ ] **Step 3: Update "Ключевые слова и тематика" section**

Add after the existing `HIGH_VALUE` description paragraph:

```markdown
Отдельная приоритетная ниша для аудитории — практический тулинг ИИ: `skill`/`скил`/`скилы` (в т.ч. `agent skills`), `prompt`/`промпт`/`промты`, `mcp`, `lifehack`/`лайфхак`/`лайфхаки` — тоже в `HIGH_VALUE`.
```

- [ ] **Step 4: Rewrite "Генерация постов (notifier.py)" section for the new format**

Replace the format block:

```markdown
Формат поста — **1 заголовок → ссылка на источник → тело → 1 CTA** (было 3 заголовка + 3 CTA — контент-менеджер получает готовый пост сразу, без выбора вариантов):

```
<Заголовок>

🔗 Источник: <url>   (📎 «Оригинальный пост» — если источник начинается с «X:», т.е. это твит)

<тело: 3-4 коротких абзаца>

<CTA>
```

Модель (`meta-llama/llama-4-scout-17b-16e-instruct`) отдаёт ответ размеченным маркерами (`ЗАГОЛОВОК:`, `ТЕЛО:`, `CTA:`), `_parse_sections()` в `generate_post()` их парсит.
```

Update the sentence about overview commands (previously implied full posts): the `/news2`/`/news24`/`/news7`/`/reddit` commands in `bot.py` now show a lightweight title + short AI description + link (via `news_pipeline.score_article_ai`), not a full ready-to-post message — the full 1-headline+CTA post is generated only via `/post <ссылка>`.

- [ ] **Step 5: Update "Автоматические рутины" section**

Add a note:

```markdown
Если рутины снова включат, `curl` должен скачивать `news_pipeline.py` вместе с `monitor.py`/`notifier.py` — без него импорт `import news_pipeline` в обоих скриптах упадёт.
```

- [ ] **Step 6: Re-read the whole file and verify it's internally consistent**

Read the full `CLAUDE.md` and confirm: the "Что это" section still matches actual behavior (on-demand `bot.py`, no scheduling), the format example matches `generate_post()`'s actual output shape from Task 8, and there's no leftover reference to "3 заголовка + 3 CTA" anywhere in the file.

- [ ] **Step 7: Commit**

```bash
git add CLAUDE.md
git commit -m "Document news_pipeline.py, new sources/keywords, and the 1+1 post format"
```

---

## Self-Review Notes

- **Spec coverage:** new sources (Task 6), full-text extraction (Task 4), structured AI scoring (Task 3), cross-source dedup (Task 2), semantic dedup (Task 5), new HIGH_VALUE keywords (Task 7/8), bot command redesign (Task 9), 1+1 post format (Task 8) — every spec section maps to a task.
- **Consistency check:** `score_article_ai` returns `AnalysisResult | None` everywhere it's called (Tasks 7, 8, 9) and every call site checks for `None` before using `.score`/`.summary`. `dedup_semantic` and `dedup_cross_source` both take/return `list[dict]` consistently across all three call sites. `_format_brief` (Task 9) and `_parse_sections`/`generate_post` (Task 8) use the same article dict shape (`title`, `url`, `source`, `verdict`, `score`) produced by Tasks 7-9's scoring loops.
- **Known scope boundary:** `bot.py`'s `fetch_url_article()` (used by `/post`) keeps its existing regex-based extraction — the spec's full-text extraction work was scoped to the scoring path in `monitor.py`/`notifier.py`/the overview commands, not to `/post`'s already-working single-article flow.
