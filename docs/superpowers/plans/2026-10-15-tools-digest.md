# Tools Digest (`/tools`) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Команда `/tools [часы]` в боте собирает дайджест 3–5 бесплатных инструментов для вайбкодеров (источники keyless, проверка по первоисточнику, один AI-вызов) и выдаёт редактору 3 сообщения: источники проверки, готовый пост для копирования, примечания.

**Architecture:** Новый `tools_digest.py` по образцу `editorial.py`: сбор из существующих fetcher-ов `news_pipeline.py` + два новых RSS (Product Hunt, DEV.to) + парсер awesome-каталогов → дедуп → проверка первоисточников → один Groq-вызов с зашитыми правилами инструкции → рендер трёх блоков. Команда и кэш — тонкий слой в `bot.py` и `get_or_build_tools()`.

**Tech Stack:** Python 3.12, urllib + feedparser (keyless), Groq API (`gpt-oss-120b`, `reasoning_effort="low"`), python-telegram-bot 22.

**Spec:** `docs/superpowers/specs/2026-10-15-tools-digest-design.md`

## Global Constraints

- Все сетевые сборщики: contract «никогда не бросает исключение, возвращает list» (spec §2).
- Всё keyless: никаких API-ключей в сборе; Groq — единственный AI (gpt-oss-120b, reasoning_effort="low").
- HTML-вывод для Telegram: всё пользовательское `html.escape`-ится, включая текст ссылки (spec §3.4, как в `notifier.assemble_digest_post`).
- Без `GROQ_API_KEY` — фолбэк «режим без AI»: голый список кандидатов, пометка, вызов не роняет (spec §7).
- Сообщения ≤ 4096 знаков, резать `_split_by_paragraphs` из `editorial.py`.
- Стиль поста: русский, «› Название [платформы] — описание», без рекламных штампов, CTA нет (spec §8-11 инструкции).
- Максимум 5 пунктов, минимум 3; меньше 3 проверенных → явная строка «подходящих инструментов недостаточно».
- `verdict`: `ok` → в пост; `uncertain` → в пост с пометкой «требует уточнения»; `wrong` → отсеян кодом.

## Review Focus

1. **Malicious URL из ленты** в `href` и в тексте ссылки поста — expect: инъекция не проходит (тест в Task 5).
2. **Awesome-README с непривычным форматом** (вложенные списки, HTML внутри, `href` без https, эмодзи в начале строки) — expect: парсер не роняет, берёт валидные `[…] (url)` пары (тест в Task 2).
3. **GitHub API 403 (rate limit)** при проверке первоисточника — expect: кандидат остаётся в списке с `uncertain`, команда не роняется (тест в Task 4).
4. **Повторный `/tools`** — expect: кэш без второго AI-вызова; другое окно — пересбор (тест в Task 6).
5. **Пустой сбор / все wrong** — expect: сообщение «инструментов не нашлось», а не пустой пост (тест в Task 5).

---

### Task 1: RSS-сборщики Product Hunt и DEV.to

**Files:**
- Modify: `news_pipeline.py` (после `fetch_rss_feed`, около строки 686)
- Test: `tests/test_news_pipeline.py`

**Interfaces:**
- Consumes: `news_pipeline.fetch_rss_feed(name, url, hours) -> tuple[list, bool]` (существует).
- Produces: `fetch_producthunt(hours=168) -> list[dict]`, `fetch_dev_to(hours=168) -> list[dict]`; константа `PH_FEED_URL`, `DEV_TO_FEED_URLS` (`["https://dev.to/feed/tag/ai", "https://dev.to/feed/tag/aitools"]`) — их потребляет Task 3.

- [ ] **Step 1: тест — фолбэк-поведение URL и контракта**

```python
def test_fetch_producthunt_and_dev_to_contract():
    """Both fetchers must wrap fetch_rss_feed: return lists, never raise."""
    import news_pipeline
    assert news_pipeline.PH_FEED_URL == "https://www.producthunt.com/feed?category=artificial-intelligence"
    # network-free: fetch via fake to check the wrapper passes hours through
    with patch.object(news_pipeline, "fetch_rss_feed") as frf:
        frf.return_value = ([{"title": "t", "url": "u", "summary": "s",
                              "source": "PH", "published": None}], True)
        arts = news_pipeline.fetch_producthunt(24)
    assert arts == frf.return_value[0]
    assert frf.call_args[0][1] == news_pipeline.PH_FEED_URL
    assert frf.call_args[0][2] == 24

    with patch.object(news_pipeline, "fetch_rss_feed") as frf:
        frf.return_value = ([], False)
        assert news_pipeline.fetch_dev_to(24) == []
        assert frf.call_count == len(news_pipeline.DEV_TO_FEED_URLS)
```

- [ ] **Step 2: прогон — падает**

Run: `python3 -m pytest tests/test_news_pipeline.py::test_fetch_producthunt_and_dev_to_contract -v`
Expected: FAIL (`AttributeError: PH_FEED_URL`)

- [ ] **Step 3: реализация**

```python
PH_FEED_URL = "https://www.producthunt.com/feed?category=artificial-intelligence"
DEV_TO_FEED_URLS = ["https://dev.to/feed/tag/ai", "https://dev.to/feed/tag/aitools"]


def fetch_producthunt(hours: int = 168) -> list[dict]:
    """Product Hunt AI feed via generic RSS wrapper. Never raises."""
    posts, _ok = fetch_rss_feed("Product Hunt", PH_FEED_URL, hours)
    return posts


def fetch_dev_to(hours: int = 168) -> list[dict]:
    """DEV Community AI-tag feed. Never raises; failures just shrink the list."""
    articles = []
    for url in DEV_TO_FEED_URLS:
        posts, _ok = fetch_rss_feed("DEV.to", url, hours)
        articles += posts
    return articles
```

- [ ] **Step 4: прогон — зелёный**

Run: `python3 -m pytest tests/test_news_pipeline.py -q`
Expected: PASS (весь файл)

- [ ] **Step 5: commit**

```bash
git add news_pipeline.py tests/test_news_pipeline.py
git commit -m "Tools: Product Hunt and DEV.to keyless RSS fetchers"
```

### Task 2: парсер awesome-каталогов

**Files:**
- Modify: `news_pipeline.py` (рядом с Task 1 кодом)
- Test: `tests/test_news_pipeline.py`

**Interfaces:**
- Consumes: `urllib.request`, `track_source_health`.
- Produces: `fetch_awesome_lists(hours=168) -> list[dict]`, константа `AWESOME_REPOS = ["taskade/awesome-vibe-coding", "yeaight7/awesome-ai-devtools"]`; `_parse_awesome_markdown(md: str) -> list[dict]` — потребляется Task 3.

- [ ] **Step 1: тесты парсера**

```python
AWESOME_FIXTURE = """# Awesome Vibe Coding
Intro text — ignored.
- [Tool A](https://github.com/a/tool-a) — CLI-сканер репо. Нужен Python 3.8+.
- [Tool B](https://b.dev) – **веб-сервис**, free tier 3 проекта.
* [Tool C](https://github.com/c/tool-c): локальная утилита сжатия
  - вложенный пункт без ссылки — игнорируется
1. [Not a link][] — пропустить
"""

def test_parse_awesome_markdown():
    import news_pipeline
    arts = news_pipeline._parse_awesome_markdown(AWESOME_FIXTURE)
    urls = [a["url"] for a in arts]
    assert urls == ["https://github.com/a/tool-a", "https://b.dev",
                    "https://github.com/c/tool-c"]
    assert arts[0]["title"] == "Tool A"
    assert "CLI-сканер" in arts[0]["summary"]
    assert arts[1]["summary"].endswith("free tier 3 проекта.")

def test_fetch_awesome_lists_never_raises_and_tracks_health():
    import news_pipeline
    with patch.object(news_pipeline.urllib.request, "urlopen",
                      side_effect=OSError("down")):
        assert news_pipeline.fetch_awesome_lists(24) == []
```

- [ ] **Step 2: прогон — падает**

Run: `python3 -m pytest tests/test_news_pipeline.py::test_parse_awesome_markdown -v`
Expected: FAIL

- [ ] **Step 3: реализация**

```python
AWESOME_REPOS = ["taskade/awesome-vibe-coding", "yeaight7/awesome-ai-devtools"]
_AWESOME_ITEM = re.compile(r'[-*]\s*\[([^\]\n]+)\]\((https?://[^)\s]+)\)\s*[-–—:]?\s*(.*)')


def _parse_awesome_markdown(md: str) -> list[dict]:
    """README item lines like "- [Name](url) — desc" into articles.
    Bad/odd lines are skipped, not fatal."""
    articles = []
    for m in _AWESOME_ITEM.finditer(md):
        title, url, desc = m.group(1).strip(), m.group(2).strip(), (m.group(3) or "")
        if not title or not url or url.startswith(("mailto:", "#")):
            continue
        articles.append({
            "title": title, "url": url,
            "summary": re.sub(r"[*_`]+", "", desc).strip()[:600],
            "source": "Awesome", "published": None,
        })
    return articles


def fetch_awesome_lists(hours: int = 168) -> list[dict]:
    """Parse awesome-list READMEs from raw.githubusercontent.com.
    Never raises; a failing repo only shrinks the list."""
    articles = []
    for repo in AWESOME_REPOS:
        name = f"Awesome: {repo}"
        try:
            req = urllib.request.Request(
                f"https://raw.githubusercontent.com/{repo}/main/README.md",
                headers={"User-Agent": _BROWSER_UA})
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = resp.read().decode("utf-8", "replace")
        except Exception as e:
            print(f"Awesome error [{name}]: {e}")
            track_source_health(name, ok=False)
            continue
        track_source_health(name, ok=True)
        articles += _parse_awesome_markdown(body)
    return articles
```

- [ ] **Step 4: прогон — зелёный**

Run: `python3 -m pytest tests/test_news_pipeline.py -q`
Expected: PASS

- [ ] **Step 5: commit**

```bash
git add news_pipeline.py tests/test_news_pipeline.py
git commit -m "Tools: awesome-list README parser"
```

### Task 3: `collect_tools`

**Files:**
- Create: `tools_digest.py`
- Test: `tests/test_tools_digest.py` (создать)

**Interfaces:**
- Consumes: `fetch_hackernews`, `fetch_github_trending`, `fetch_hf_trending`, `fetch_reddit`, `dedup_cross_source` из `news_pipeline`; `fetch_producthunt`, `fetch_dev_to`, `fetch_awesome_lists` (Task 1–2); константу `AWESOME_REPOS`.
- Produces: `TOOL_KEYWORDS: list[str]`, `_kw_matches` — реэкспорт из notifier; `collect_tools(hours) -> list[dict]`, `CANDIDATE_CAP = 40`, `TOOL_SCORES`-вес ключей `TOOL_KEYWORD_WEIGHTS`. Потребляется Task 4–6.

- [ ] **Step 1: тест**

```python
def test_collect_tools_merges_dedups_filters_caps():
    import tools_digest
    def art(url, i):
        return {"title": "Tool", "url": url, "summary": "CLI утилита",
                "source": "S", "published": None}
    feeds = {
        "hn": [art(f"https://x/{i}", i) for i in range(5)],
        "dup": [art("https://x/0", 0)],  # дубль — склеится
        "irrelevant": [{"title": "Кино", "url": "https://k/1",
                        "summary": "премьера фильма", "source": "Z",
                        "published": None}],
    }
    def fake_hn(hours=168, **kw): return feeds["hn"]
    with patch.object(tools_digest.news_pipeline, "fetch_hackernews", fake_hn), \
         patch.object(tools_digest.news_pipeline, "fetch_github_trending", return_value=[]), \
         patch.object(tools_digest.news_pipeline, "fetch_hf_trending", return_value=[]), \
         patch.object(tools_digest.news_pipeline, "fetch_reddit", return_value=[]), \
         patch.object(tools_digest.news_pipeline, "fetch_producthunt", return_value=feeds["dup"]), \
         patch.object(tools_digest.news_pipeline, "fetch_dev_to", return_value=[]), \
         patch.object(tools_digest.news_pipeline, "fetch_awesome_lists", return_value=[]):
        got = tools_digest.collect_tools(168)
    assert len(got) <= tools_digest.CANDIDATE_CAP
    assert not any(a["url"] == "https://k/1" for a in got)  # irrelevant отфильтрован по keyword
```

- [ ] **Step 2: прогон — падает**

Run: `python3 -m pytest tests/test_tools_digest.py::test_collect_tools_merges_dedups_filters_caps -v`
Expected: FAIL (`ModuleNotFoundError: tools_digest`)

- [ ] **Step 3: реализация**

```python
"""Tools digest pipeline: collect → check official pages → one AI selection."""
from __future__ import annotations

from notifier import KEYWORDS, _kw_matches, score_article
import news_pipeline

CANDIDATE_CAP = 40

TOOL_KEYWORDS = KEYWORDS + [
    "CLI", "extension", "плагин", "расширени", "конструктор", "converter",
    "конвертер", "tool", "утилит", "офлайн", "offline", "open source",
    "open-source", "opensource", "chrome", "браузер", "browser", "бета",
    "бесплатн", "free", "автоматиз", "скрипт", "генератор", "парсер",
    "конверт", "документ", "PDF", "скриншот", "вниз по весам", "download",
    "Show HN",
]

TOOL_KEYWORD_WEIGHTS = {"Show HN": 3, "CLI": 2, "утилит": 2, "бесплатн": 1}


def _tool_score(article: dict) -> int:
    text = (article["title"] + " " + article["summary"]).lower()
    score = 0
    for kw in TOOL_KEYWORDS:
        if _kw_matches(kw, text):
            score += TOOL_KEYWORD_WEIGHTS.get(kw, 1)
    return score


def collect_tools(hours: int = 168) -> list[dict]:
    """Merge all tool sources, dedup, keyword-filter, cap by score.
    Never raises (each fetcher is individually safe)."""
    articles = news_pipeline.fetch_hackernews(hours=hours, min_score=50)
    articles += news_pipeline.fetch_github_trending(hours=hours)
    articles += news_pipeline.fetch_hf_trending()
    articles += news_pipeline.fetch_reddit(
        subreddits=["LocalLLaMA", "opensource", "SideProject",
                    "vibecoding", "ClaudeAI"], hours=hours)
    articles += news_pipeline.fetch_producthunt(hours=hours)
    articles += news_pipeline.fetch_dev_to(hours=hours)
    articles += news_pipeline.fetch_awesome_lists(hours=hours)
    articles = news_pipeline.dedup_cross_source(articles)
    relevant = [a for a in articles if _tool_score(a) > 0]
    return sorted(relevant, key=_tool_score, reverse=True)[:CANDIDATE_CAP]
```

(Подсказка исполнителью: `from notifier import KEYWORDS...` — проверить, что `KEYWORDS` в notifier включает ключи типа `tool` — иначе расширить список в `notifier.py` НЕ надо: расширяем только локальный `TOOL_KEYWORDS`.)

- [ ] **Step 4: прогон**

Run: `python3 -m pytest tests/test_tools_digest.py -q`
Expected: PASS

- [ ] **Step 5: commit**

```bash
git add tools_digest.py tests/test_tools_digest.py
git commit -m "Tools: candidate collection (collect_tools)"
```

### Task 4: проверка первоисточника

**Files:**
- Modify: `tools_digest.py`
- Test: `tests/test_tools_digest.py`

**Interfaces:**
- Consumes: `news_pipeline.extract_full_text(url) -> Optional[str]` (существует), `urllib.request`.
- Produces: `fetch_official_page(article: dict) -> dict` — возвращает `{"text": Optional[str], "readme": Optional[str], "license": Optional[str], "last_release": Optional[str], "status": "checked|uncertain"}`, и `prepare_candidates(cands: list[dict]) -> list[dict]` — каждый кандидат получает `_official` (его потребляет Task 5).

- [ ] **Step 1: тесты (GitHub enrichment + 403 → uncertain)**

```python
def test_fetch_official_page_github_enrichment():
    """GitHub URL: README + license + release pulled; page with readme → checked."""
    import tools_digest
    pages = {
        "https://raw.githubusercontent.com/a/b/main/README.md":
            b"# TOOL A readme body with install steps",
        "https://api.github.com/repos/a/b/releases/latest":
            json.dumps({"tag_name": "v1.2.0"}).encode(),
        "https://api.github.com/repos/a/b/license":
            json.dumps({"license": {"spdx_id": "MIT"}}).encode(),
    }
    def fake_urlopen(req, timeout=15):
        class R:
            status = 200
            def read(self): return pages[req.full_url]
            def __enter__(self): return self
            def __exit__(self, *a): return False
        return R()
    with patch.object(tools_digest.urllib.request, "urlopen", fake_urlopen):
        art = {"title": "TOOL A", "url": "https://github.com/a/b",
               "summary": "CLI утилита", "source": "X", "published": None}
        info = tools_digest.fetch_official_page(art)
    assert "readme body" in info["readme"]
    assert info["license"] == "MIT"
    assert info["last_release"] == "v1.2.0"
    assert info["status"] == "checked"

def test_github_rate_limited_marks_uncertain():
    import tools_digest
    with patch.object(tools_digest.urllib.request, "urlopen",
                      side_effect=Exception("HTTP Error 403")):
        art = {"title": "t", "url": "https://github.com/a/b", "summary": "s",
               "source": "X", "published": None}
        info = tools_digest.fetch_official_page(art)
    assert info["status"] == "uncertain"  # НЕ отбрасывается
```

- [ ] **Step 2: прогон — падает**

Run: `python3 -m pytest tests/test_tools_digest.py -k official -v`
Expected: FAIL

- [ ] **Step 3: реализация**

```python
def fetch_official_page(article: dict) -> dict:
    """Pull the official page (and, for GitHub repos, README/license/release).
    A failed check marks the candidate uncertain — never drops it."""
    info: dict = {"text": None, "readme": None, "license": None,
                  "last_release": None, "status": "uncertain"}
    url = article.get("url", "")
    try:
        info["text"] = news_pipeline.extract_full_text(url)
    except Exception:
        pass
    m = re.match(r"https://github\.com/([^/]+)/([^/#?]+)", url)
    if m and not "/tree/" in url and not url.endswith(".md"):
        user, repo = m.group(1), m.group(2)
        # Кандидаты с awesome-листа иногда ведут на сайт, а не на GitHub:
        # тогда README берём только если url — github.
        info["readme"] = _gh_get(f"https://raw.githubusercontent.com/{user}/{repo}/main/README.md", "text")
        rel = _gh_json(f"https://api.github.com/repos/{user}/{repo}/releases/latest")
        if rel:
            info["last_release"] = rel.get("tag_name")
            lic = None
        licenses = _gh_json(f"https://api.github.com/repos/{user}/{repo}/license")
        if licenses:
            info["license"] = licenses.get("license", {}).get("spdx_id")
        if info["readme"] or info["text"]:
            info["status"] = "checked"
    elif info["text"]:
        info["status"] = "checked"
    return info


def _gh_get(url: str, kind: str) -> Optional[str]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _BROWSER_UA})
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read()
        if resp.status >= 400: return None
        return body.decode("utf-8", "replace") if kind == "text" else _json.loads(body)
    except Exception:
        return None


def _gh_json(url: str) -> Optional[dict]:
    return _gh_get(url, "json")


def prepare_candidates(cands: list[dict]) -> list[dict]:
    """Attach _official info to each candidate (sequential, GitHub-safe pacing)."""
    for c in cands:
        c["_official"] = fetch_official_page(c)
    return cands
```

- [ ] **Step 4: прогон**

Run: `python3 -m pytest tests/test_tools_digest.py -q`
Expected: PASS

- [ ] **Step 5: commit**

```bash
git add tools_digest.py tests/test_tools_digest.py
git commit -m "Tools: official-page verification (fetch_official_page)"
```

### Task 5: `select_tools` — один AI-вызов + вердикты

**Files:**
- Modify: `tools_digest.py`
- Test: `tests/test_tools_digest.py`

**Interfaces:**
- Consumes: `prepare_candidates` (Task 4), правила инструкции (разделы §3 spec §3.3).
- Produces: `select_tools(candidates, client=None) -> dict` —
  `{"title", "items": [{"name", "url", "category", "platforms", "free_tier",
  "desc", "limits", "proof_url", "verdict"}], "notes", "mode": "ai|fallback"}`
  — потребляет Task 6. Вспомогательные: `_parse_tools_json(raw) -> Optional[dict]`.

- [ ] **Step 1: фейк-клиент + тесты**

```python
_TOOLS_RAW = json.dumps({
 "title": "Инструменты недели",
 "items": [
  {"name": "Tool A", "url": "https://github.com/a/a",
   "category": "CLI", "platforms": "macOS, Linux", "free_tier": "open-source",
   "desc": "Быстро чистит папки проектов от артефактов сборки.",
   "limits": "Нужен Python 3.8+", "proof_url": "https://github.com/a/a#readme",
   "verdict": "ok"},
  {"name": "Tool B", "url": "https://b.dev", "category": "Web",
   "platforms": "Web", "free_tier": "free tier",
   "desc": "Сканер безопасности.", "limits": "", "proof_url": "https://b.dev/docs",
   "verdict": "uncertain"},
  {"name": "Tool C", "url": "https://c.app", "category": "Web",
   "platforms": "Web", "free_tier": "не проверено",
   "desc": "Выдуман.", "limits": "", "proof_url": "",
   "verdict": "wrong"}],
 "notes": "лимитов нет"}, ensure_ascii=False)


def test_select_tools_verdicts_and_cap():
    import tools_digest
    cands = tools_digest.prepare_candidates(_fake_articles(8))
    client = _fake_client(_TOOLS_RAW, content=lambda x: _TOOLS_RAW)
    result = tools_digest.select_tools(cands, client)
    names = [it["name"] for it in result["items"]]
    assert "Tool C" not in names            # wrong отсеян
    assert result["items"][0]["verdict"] == "ok"
    assert result["mode"] == "ai"
    assert len(result["items"]) <= 5

def test_select_tools_all_wrong_falls_back():
    """Every item wrong → no items → fallback candidates, not an empty post."""
    import tools_digest
    all_wrong = json.dumps({"title": "T",
        "items": [{"name": "X", "url": "https://x.dev", "category": "Web",
                   "platforms": "Web", "free_tier": "?", "desc": "d",
                   "limits": "", "proof_url": "", "verdict": "wrong"}] * 3},
        ensure_ascii=False)
    cands = tools_digest.prepare_candidates(_fake_articles(4))
    result = tools_digest.select_tools(cands, _fake_client(all_wrong, content=lambda x: all_wrong))
    assert result["mode"] == "fallback"
    assert result["items"]

def test_select_tools_without_ai_fallback():
    import tools_digest
    cands = tools_digest.prepare_candidates(_fake_articles(8))
    result = tools_digest.select_tools(cands, client=None)
    assert result["mode"] == "fallback"
    assert all("name" in it and "url" in it for it in result["items"])

def test_select_tools_parse_garbage_falls_back():
    import tools_digest
    cands = tools_digest.prepare_candidates(_fake_articles(4))
    client = _fake_client("не JSON", content=lambda x: "```json\n{bad")
    result = tools_digest.select_tools(cands, client)
    assert result["mode"] == "fallback"
```

(Хелперы `_fake_articles(n)` — список n статей со смешанных URL github/не-github, `summary` содержат CLI-ключевое слово; `_fake_client(raw, ...)` — тот же паттерн, что `_fake_client` в `tests/test_editorial.py:60` — копируй его.)

- [ ] **Step 2: прогон — падает**

Run: `python3 -m pytest tests/test_tools_digest.py -k select -v`
Expected: FAIL

- [ ] **Step 3: реализация**

```python
_MAX_TEXT = 1500

def select_tools(candidates: list[dict], client=None) -> dict:
    """One Groq call: score candidates against the editor instructions,
    return up to 5 diverse items with verdicts. No AI → fallback list."""
    if not client or not candidates:
        return _fallback_tools(candidates)
    cand_lines = []
    for c in candidates:
        off = c.get("_official") or {}
        text = off.get("readme") or off.get("text") or c["summary"] or ""
        cand_lines.append(
            f"- имя: {c['title']}\n  url: {c['url']}\n  источник: {c['source']}\n"
            f"  официальный текст: {text[:_MAX_TEXT]}\n"
            f"  github: readme={'да' if off.get('readme') else 'нет'}, "
            f"license={off.get('license')}, last_release={off.get('last_release')}")
    prompt = _TOOLS_PROMPT.format(n=len(candidates), cands="\n".join(cand_lines))
    try:
        response = client.chat.completions.create(
            model="openai/gpt-oss-120b", reasoning_effort="low",
            messages=[
                {"role": "system", "content": _TOOLS_SYSTEM_RULES},
                {"role": "user", "content": prompt},
            ], temperature=0.2)
        raw = response.choices[0].message.content
        parsed = _parse_tools_json(raw)
    except Exception as e:
        print(f"Tools AI error: {e}")
        parsed = None
    if parsed and parsed.get("items"):
        items = [it for it in parsed["items"] if it.get("verdict") != "wrong"][:5]
        if items:
            parsed["items"] = items
            parsed["mode"] = "ai"
            return parsed
    return _fallback_tools(candidates)


def _fallback_tools(candidates: list[dict]) -> dict:
    """No-API mode: bare candidate list, no promises about free tiers."""
    items = [{"name": c["title"][:60], "url": c["url"], "category": "—",
              "platforms": "не проверено", "free_tier": "не проверено",
              "desc": (c.get("summary") or "").strip()[:120],
              "limits": "", "proof_url": "", "verdict": "uncertain"}
             for c in candidates[:5]]
    return {"title": "Кандидаты инструментов (режим без AI)", "items": items,
            "notes": "Требует ручной проверки: описания — из RSS, не с офиц. страниц.",
            "mode": "fallback"}
```

`_TOOLS_SYSTEM_RULES` — многострочная константа, дословно включающая правила инструкции: роль, категории, приоритеты (неочевидные находки, бесплатные альтернативы, применить сразу, релизы, open-source), 5 моделей бесплатности с запретом преувеличения, запрет рекламных штампов и категоричных заявлений, разнообразие, «если мало проверенных — вернуть меньше, не заполнять пробелы». `_TOOLS_PROMPT` — формат JSON-ответа (строго как в §spec 3.3) поверх `cands` с нумерацией.

- [ ] **Step 4: прогон**

Run: `python3 -m pytest tests/test_tools_digest.py -q`
Expected: PASS

- [ ] **Step 5: commit**

```bash
git add tools_digest.py tests/test_tools_digest.py
git commit -m "Tools: one Groq selection call with verdict scheme"
```

### Task 6: рендер трёх блоков

**Files:**
- Modify: `tools_digest.py`
- Test: `tests/test_tools_digest.py`

**Interfaces:**
- Consumes: `select_tools` (Task 5), `_split_by_paragraphs` из `editorial.py:442`.
- Produces: `render_tools_sources(result) -> str`, `render_tools_post(result) -> str`, `render_tools_notes(result) -> str` — потребляет Task 7. Формат поста — `› <a href="...">Название</a> [платформы] — описание` (+ «⚠️ Огра́ничение: …» при непустом `limits`), заголовок как `<b>...</b>`.

- [ ] **Step 1: тесты**

```python
def test_render_tools_post_format_and_escape():
    import tools_digest
    result = tools_digest.select_tools(tools_digest.prepare_candidates(
        _fake_articles(8)), _fake_client(_TOOLS_RAW, content=lambda x: _TOOLS_RAW))
    post = tools_digest.render_tools_post(result)
    assert "<b>Инструменты недели</b>" in post
    assert "›" in post and "<a href=" in post
    assert "⚠️" in post or "Ограничение" not in post  # limits непустые → есть
    # эскейп
    result2 = dict(result)
    result2["items"] = [dict(result["items"][0],
                             name="Скрипт <b>жирный</b>")]
    post2 = tools_digest.render_tools_post(result2)
    assert "<b>жирный</b>" not in post2 and "&lt;b&gt;жирный" in post2

def test_render_tools_malicious_url_cannot_inject():
    import tools_digest
    result = {"title": "T", "mode": "ai", "notes": "n",
              "items": [{"name": "H", "url": 'https://a.example"></a><a href="https://phish.example">',
                         "category": "Web", "platforms": "Web",
                         "free_tier": "free", "desc": "d", "limits": "",
                         "proof_url": "https://safe.example/doc",
                         "verdict": "ok"}]}
    post = tools_digest.render_tools_post(result)
    assert 'href="https://phish.example' not in post
    sources = tools_digest.render_tools_sources(result)
    assert 'href="https://phish.example' not in sources

def test_render_tools_insufficient_warning():
    import tools_digest
    result = {"title": "T", "mode": "ai", "notes": "n",
              "items": [_ok_item()]}  # ровно 1 проверенный
    post = tools_digest.render_tools_post(result)
    assert "недостаточно" in post

def test_render_tools_split_at_limit():
    """Long posts cut by _split_by_paragraphs like editor cards do."""
    import tools_digest
    items = [_ok_item(desc="x" * 4500, i=i) for i in range(5)]
    result = {"title": "T", "mode": "ai", "notes": "n", "items": items}
    msgs = tools_digest.build_tools_messages(result)
    assert len(msgs) >= 2
    assert all(len(m) <= 4096 for m in msgs)
```

(`_ok_item(desc=None, i=0)` — хелпер по контракту Task 5:
```python
def _ok_item(desc="Важная утилита, которую можно попробовать сразу.", i=0):
    return {"name": f"Tool {chr(65 + i)}", "url": f"https://t{i}.example",
            "category": "CLI", "platforms": "macOS, Linux",
            "free_tier": "open-source", "desc": desc,
            "limits": "Нужен Python 3.8+", "proof_url": f"https://t{i}.example/doc",
            "verdict": "ok"}
```
)

- [ ] **Step 2: прогон — падает**

Run: `python3 -m pytest tests/test_tools_digest.py -k render -v`
Expected: FAIL

- [ ] **Step 3: реализация**

```python
def render_tools_post(result: dict) -> str:
    parts = [f"<b>{html.escape(result['title'])}</b>"]
    checked = [it for it in result["items"] if it["verdict"] != "wrong"]
    if len(checked) < 3:
        parts.append("⚠️ Проверенных инструментов недостаточно — покажу только их:")
    for it in result["items"]:
        url = it.get("url") or ""
        name = f'› <a href="{html.escape(url, quote=True)}">{html.escape(it["name"])}</a>'
        line = f"{name} [{html.escape(it.get('platforms') or '')}] — {html.escape(it['desc'])}"
        if it.get("limits"):
            line += f" (⚠️ {html.escape(it['limits'])})"
        if it.get("verdict") == "uncertain":
            line += "\n<i>требует уточнения</i>"
        parts.append(line)
    return "\n\n".join(parts)


def render_tools_sources(result: dict) -> str:
    lines = ["🔎 <b>Источники проверки</b>"]
    for it in result["items"]:
        lines.append(f"• {html.escape(it['name'])}: {html.escape(it.get('proof_url') or it['url'])}")
    return "\n".join(lines)


def render_tools_notes(result: dict) -> str:
    notes = [html.escape(result.get("notes") or "—")]
    if result.get("mode") == "fallback":
        notes.append("Режим без AI: не делай выводов о бесплатности без проверки.")
    return "\n".join(notes)


def build_tools_messages(result: dict) -> list[str]:
    """Three ready blocks for the bot, each ≤ 4096."""
    from editorial import _split_by_paragraphs
    return (_split_by_paragraphs(render_tools_sources(result))
            + _split_by_paragraphs(render_tools_post(result))
            + _split_by_paragraphs(render_tools_notes(result)))
```

- [ ] **Step 4: прогон**

Run: `python3 -m pytest tests/test_tools_digest.py -q`
Expected: PASS (полный файл: 3 предыдущих фикса-теста + новый split-тест)

- [ ] **Step 5: commit**

```bash
git add tools_digest.py tests/test_tools_digest.py
git commit -m "Tools: render sources/post/notes blocks with 4096 split"
```

### Task 7: `get_or_build_tools` + бот-команда

**Files:**
- Modify: `tools_digest.py` (кэш), `bot.py` (команда + меню + импорт)
- Test: `tests/test_tools_digest.py`

**Interfaces:**
- Consumes: `collect_tools`, `prepare_candidates`, `select_tools`, `build_tools_messages` (Task 3–6), фейк-клиент.
- Produces: `get_or_build_tools(store: dict, hours: int, client=None) -> list[str]` — store — это `bot_data`; ключи `tools_cache`, `tools_seen`. В `bot.py`: `cmd_tools(update, context)`, пункт меню.

- [ ] **Step 1: тест кэша и seen**

```python
def test_get_or_build_tools_cache_and_seen():
    import tools_digest
    store: dict = {}
    client = _fake_client(_TOOLS_RAW, content=lambda x: _TOOLS_RAW)

    def fresh_cands(hours, **kw):
        return [_fake_article(f"https://new{i}.example/1", i) for i in range(6)]

    with patch.object(tools_digest, "collect_tools", side_effect=fresh_cands) as ct, \
         patch.object(tools_digest.news_pipeline, "extract_full_text", return_value=None):
        msgs1 = tools_digest.get_or_build_tools(store, 168, client)
        assert ct.call_count == 1
        assert store["tools_cache"]["hours"] == 168
        # повторный вызов — то же окно → кэш, сбор не запускается второй раз
        msgs2 = tools_digest.get_or_build_tools(store, 168, client)
        assert ct.call_count == 1
        assert msgs1 == msgs2
        # другое окно → пересбор; уже показанные url исключены через tools_seen
        ok_urls = {it["url"] for it in json.loads(_TOOLS_RAW)["items"]}
        assert store["tools_seen"] & ok_urls == ok_urls
        with patch.object(tools_digest, "collect_tools",
                          side_effect=lambda h, **kw: [_fake_article(u, i)
                             for i, u in enumerate(ok_urls)]):
            msgs3 = tools_digest.get_or_build_tools(store, 24, client)
        # все кандидаты уже показаны → явное сообщение, а не пустой пост
        assert msgs3 == ["Инструментов не нашлось — попробуй: /tools 720"]
```

(`_fake_article(url, i)` — базовый хелпер Task 5: dict статья с CLI-ключевым словом в `summary`.)

- [ ] **Step 2: прогон — падает**

Run: `python3 -m pytest tests/test_tools_digest.py -k cache -v`
Expected: FAIL

- [ ] **Step 3: реализация в tools_digest.py**

```python
def get_or_build_tools(store: dict, hours: int, client=None) -> list[str]:
    """Cache of the last run in bot_data-shaped dict; builds on miss or window change."""
    cache = store.setdefault("tools_cache", {})   # {"hours": int, "result": dict}
    seen: set = store.setdefault("tools_seen", set())
    if cache and cache.get("hours") == hours:
        return build_tools_messages(cache["result"])
    cands = [c for c in collect_tools(hours) if normalize_url(c["url"]) not in seen]
    if not cands:
        return ["Инструментов не нашлось — попробуй: /tools 720"]
    result = select_tools(prepare_candidates(cands[:20]), client)
    urls = {normalize_url(it["url"]) for it in result.get("items", [])
            if it.get("verdict") == "ok"}
    store["tools_seen"] |= urls
    store["tools_cache"] = {"hours": hours, "result": result}
    return build_tools_messages(result)
```

(Проверь: `normalize_url` — импорт из `news_pipeline` (обрезает query/фрагменты; если вернёт None — пропускать кандидата). `cands[:20]` — ограничение для AI-вызова: 20 лучших; на GitHub-проверку уходят только они.)

- [ ] **Step 4: команда в bot.py**

Рядом с `cmd_digest` (строка 587), по её же образцу (status reply → логика → edit):

```python
async def cmd_tools(update, context) -> None:
    hours = 168
    if context.args:
        try:
            hours = min(max(int(context.args[0]), 6), 720)
        except ValueError:
            pass
    status = await update.message.reply_text(
        f"Собираю инструменты за {hours} ч… это 1–3 минуты")
    client = _groq_client()
    msgs = tools_digest.get_or_build_tools(context.bot_data, hours, client)
    for m in msgs:
        await send_long_html(update, m)
```

(Если в bot.py нет `send_long_html` — использовать тот же паттерн, что в `cmd_digest`/`cmd_card`: `reply_text(..., parse_mode="HTML", disable_web_page_preview=True)` порционно по блокам, которые уже ≤ 4096.)

Регистрация: `app.add_handler(CommandHandler("tools", cmd_tools))` (после card) и `BotCommand("tools", "🧰 Дайджест бесплатных инструментов: /tools")` в `post_init`.

- [ ] **Step 5: полные тесты**

Run: `python3 -m pytest tests/ -q`
Expected: PASS (все, включая существующие)

- [ ] **Step 6: commit**

```bash
git add tools_digest.py bot.py tests/
git commit -m "Bot: /tools command with cache and seen-tools exclusion"
```

### Task 8: документация и деплой

**Files:**
- Modify: `CLAUDE.md` (раздел «Карточки для редактора» → рядом «Дайджест инструментов (/tools)»), `README.md` (список команд бота)
- Ops: сервер, вне репо

- [ ] **Step 1: доки** — короткий раздел в CLAUDE.md по образцу раздела editorial (`/digest`): источники, один AI-вызов, вердикты ok/uncertain/wrong, кэш и tools_seen, фолбэк без ключа; README — строка команды. Коммит `Docs: /tools digest`.

- [ ] **Step 2: push, затем на сервер** —

```bash
ssh kate_dmitr@193.124.80.196 "cd /opt/antispam_bot/news_parser && \
  curl -fsSL -o tools_digest.py https://raw.githubusercontent.com/rtenesha/ai-news-monitor/main/tools_digest.py && \
  curl -fsSL -o news_pipeline.py https://raw.githubusercontent.com/rtenesha/ai-news-monitor/main/news_pipeline.py && \
  curl -fsSL -o bot.py https://raw.githubusercontent.com/rtenesha/ai-news-monitor/main/bot.py"
```

Перезапуск бота: kill по пути `venv/bin/python bot.py` (cwd-проверка против чужих проектов), старт одного экземпляра `setsid nohup`, проверка лога «Бот запущен» и отсутствия новых Conflict. SSH флапает — при соединении «banner exchange timeout» повторять с паузой 50с.

- [ ] **Step 3: живая проверка** — `/tools` в Telegram: три сообщени (источники, пост, примечания), пункты со ссылками; второй вызов возвращает кэш. При сбое см. `logs/bot.log` на сервере.

---

## Handoff notes for executors

- Модель вызова: копируй `client.chat.completions.create(model="openai/gpt-oss-120b", reasoning_effort="low", ...)` из `editorial.py:155-160` — тот же паттерн.
- Фейк-клиент: в `tests/test_editorial.py` уже есть `_fake_client` (строка ~60) — копируй в test_tools_digest, не импортируй из теста в тест.
- `_kw_matches`, `score_article`, `KEYWORDS` живут в `notifier.py` — их импорт туда уже отработан в `editorial.py:14-15`, повтори паттерн.
- Не трогай: `notifier._is_hot`, `editorial.select_topics`, `KEYWORDS`/`HIGH_VALUE` в notifier — они для новостей дайджеста.