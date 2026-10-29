# Pain Points Digest (`/pain`) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Команда `/pain [часы]` собирает обсуждения HN + Reddit за окно, читает их комментарии (keyless Firebase / Reddit JSON), один AI-вызов группирует вопросные/проблемные фрагменты в ≤ 6 карточек редакционных возможностей (проблема, категория, хук, решение, доказательства-ссылки на комментарии) и выдаёт редактору 2 блока + статусная команда `/pain_set`.

**Architecture:** Новый `pain_digest.py` по образцу `tools_digest.py`: сбор обсуждений из Firebase (topstories + askstories) и Reddit `/new.json` → dedup → keyword-фильтр → комментарии с капами → regex-фрагменты → один Groq-вызов → провенанс-проверка цитат кодом → рендер. Команды, кэш и статусы — тонкий слой в `bot.py` (`bot_data`-ключи `pain_cache`, `pain_seen`, `pain_status`, `pain_counter`, `pain_display_map`).

**Tech Stack:** Python, stdlib urllib + ThreadPoolExecutor (keyless), Groq (`openai/gpt-oss-120b`, `reasoning_effort="low"`), python-telegram-bot 22. Algolia не используется (недоступен с сети сервера — проверено спайком).

**Spec:** `docs/superpowers/specs/2026-10-16-pain-points-digest-design.md`

## Global Constraints

- Сетевые сборщики — контракт «никогда не бросает исключение, возвращает list», каждый запрос с ретраем.
- Всё, кроме AI-вызова, keyless. AI: `openai/gpt-oss-120b`, `reasoning_effort="low"`, один вызов на прогон, temperature=0.2.
- Капы: HN topstories 60 id (score ≥ 100) + askstories (score ≥ 50); Reddit score ≥ 20 ИЛИ num_comments ≥ 10; 12 веток × 2 уровня, 40 комм. HN / 30 комм. Reddit на обсуждение; 16 обсуждений на прогон (8 HN + 8 Reddit); 50 фрагментов × 400 знаков; ≤ 6 карточек; ≤ 3 доказательства в отрисовке.
- Окно по умолчанию 48 ч, `/pain N` — 6–168. Кэш отдаётся только при совпадении окна (как `tools_cache`).
- Цитаты и `comment_url` — только из переданного набора фрагментов; `evidence_count` пересчитывается кодом (число модели игнорируется); уникальность — по `discussion_url`.
- JSON ответ модели: `cards` обязателен список, поля принудительно приводятся к строке; неизвестные `category` → «другое», неизвестный `solution_status` → «не найдено»; `wrong`-карточки отбрасываются.
- Русский текст (`essence`/`hook`/`problem`) без ИИ-штампов (`notifier._NATURAL_TEXT_RULES`); цитаты — дословные, оригинальные.
- Telegram-вывод: `html.escape` всего пользовательского, блоки ≤ 4096, резать `_split_by_paragraphs` из `editorial.py`; без отправки в канал; `TELEGRAM_AUTO_SEND` не гейтит.
- Без `GROQ_API_KEY` — фолбэк: regex-фрагменты списком с пометкой «режим без AI».
- Комментарии кода — по стилю `tools_digest.py` (кратко, по-русски, только где неочевидно).

## Review Focus

1. **Выдуманная моделью цитата/url** — expect: карточка выброшена целиком, ссылка вспоминается в «rejected» как в `tools_digest` (тест в Task 4).
2. **Reddit JSON 403/таймаут** — expect: reddit-стадия отключается до перезапуска, прогон живёт на HN, команда не роняется (тесты в Tasks 1, 2).
3. **Deleted/dead HN-комментарии и пустой `text`** — expect: пропущены, не в фрагментах, счёт капа не съедают (тест в Task 2).
4. **Неизвестная категория/статус решения/cards не список** — expect: нормализация, не краш; мусорная структура JSON → фолбэк (тест в Task 4).
5. **HTML-инъекция в цитате/заголовке и повторный `/pain`** — expect: экранируется; кэш без второго AI-вызова, другое окно — пересбор (тесты в Tasks 5, 6).

---

### Task 1: Сбор обсуждений — `collect_discussions`

**Files:**
- Create: `pain_digest.py` (новый модуль; заголовок-докстринг как в `tools_digest.py`)
- Test: `tests/test_pain_digest.py` (новый)

**Interfaces:**
- Consumes: `news_pipeline.fetch_hackernews` НЕ переиспользуем напрямую (нужен доступ к story-объектам с `kids`/`text` — поэтому собственная загрузка через тот же Firebase base URL `news_pipeline` не экспортирует: заводим в модуле `HN_BASE = "https://hacker-news.firebaseio.com/v0"`); `notifier.KEYWORDS, notifier._kw_matches, notifier.score_article` (существуют); `news_pipeline.dedup_cross_source(articles) -> list`, `news_pipeline.normalize_url(url) -> str` (существуют).
- Produces: `DEFAULT_HOURS: int = 48`, `collect_discussions(hours: int) -> list[dict]`; обсуждение-словарь `{"title", "url", "summary", "source", "published", "score", "num_comments", "hn_id": Optional[int], "reddit_fullname": Optional[str], "_story": dict|None, "_permalink": str|None}` — потребляют Tasks 2, 4, 6. Также `_PAIN_MARKERS: re.Pattern`, `_pain_score(article) -> int`, `PAIN_DISCUSSION_CAP = 16`, `REDDIT_DISCUSSION_CAP = 8`, `HN_DISCUSSION_CAP = 8`.

- [ ] **Step 1: тесты — HN (topstories+askstories), Reddit-отбор, деградация**

```python
def _hn_story(sid, score, title, text=None):
    return {"id": sid, "score": score, "title": title, "text": text,
            "time": __import__("time").time() - 100, "url": None, "kids": []}


def test_collect_discussions_hn_top_and_ask():
    import pain_digest, news_pipeline
    top = _hn_story(101, 150, "Show HN: vibe coding tool")
    ask = _hn_story(102, 70, "Ask HN: how do you keep agents in scope?",
                    text="how do you keep agents from touching extra files?")
    low = _hn_story(103, 30, "Unrelated low-score story")
    with patch.object(pain_digest, "_hn_get_json") as g, \
         patch.object(pain_digest, "_fetch_reddit_discussions", return_value=[]):
        g.side_effect = [[101, 102, 103],       # topstories.json
                         top, ask, low,          # item/<id>.json
                         [103, 104],             # askstories.json
                         top, top]               # item fetches again
        got = pain_digest.collect_discussions(48)
    urls = [news_pipeline.normalize_url(d["url"]) for d in got]
    assert any(d["hn_id"] == 102 for d in got)        # ask (70) прошёл
    assert not any(d["hn_id"] == 103 for d in got)    # 30 < 50 → отфильтрован
    assert any(d["_story"] is not None for d in got)   # story передан дальше
    assert len(got) <= pain_digest.PAIN_DISCUSSION_CAP


def test_collect_discussions_reddit_selection_and_degrade():
    import pain_digest
    good = _reddit_post(score=25, num_comments=2)     # score ≥ 20 → годен
    chatty = _reddit_post(score=3, num_comments=12)   # comments ≥ 10 → годен
    weak = _reddit_post(score=3, num_comments=2)      # оба порога не прошёл
    with patch.object(pain_digest, "_reddit_new_json", side_effect=[[good, chatty, weak]]):
        pain_digest._REDDIT_JSON_DISABLED = False
        got = pain_digest._fetch_reddit_discussions(48)
        assert {d["reddit_fullname"] for d in got} == {good["name"], chatty["name"]}
    # 403 → стадия отключается на время процесса
    pain_digest._REDDIT_JSON_DISABLED = True
    with patch.object(pain_digest, "_reddit_new_json") as nj:
        assert pain_digest._fetch_reddit_discussions(48) == []
        assert nj.call_count == 0                    # флаг сразу отключает запросы
    pain_digest._REDDIT_JSON_DISABLED = False  # не тащить состояние в другие тесты
```

(Сигнатуры-помощники `_hn_story`/`_reddit_post` — фикстуры теста; реальные ассерты проверяют пороги score/num_comments, keyword-фильтр, кап 16 и флаг `_REDDIT_JSON_DISABLED` после 403.)

- [ ] **Step 2: прогнать тест — фиксация**

Run: `pytest tests/test_pain_digest.py -v`
Expected: FAIL (модуль не существует; `ModuleNotFoundError`)

- [ ] **Step 3: реализовать в `pain_digest.py`**

- `_hn_get_json(path: str, timeout: int = 12) -> Optional[Any]` — 1 ретрай (+1s сон); `HN_BASE`.
- HN: `topstories.json` → `[:60]` × item (score ≥ 100) + `askstories.json` → `[:30]` × item (score ≥ 50); окно-фильтр `published` по unix `time`; через `ThreadPoolExecutor(max_workers=8)`. Story → обсуждение: `title`, `url = story["url"] or f"{item_url}"`, `summary = (story["text"] or "")[:600]`, `source = "Hacker News"`, `hn_id`, `_story = story`.
- Reddit: при `_reddit_reachable()` (module-флаг, не disabled) — `/r/<sub>/new.json?limit=50` (`REDDIT_SUBREDDITS`, пауза 2с между сабами, браузерный UA `_BROWSER_UA`); post → обсуждение (`score ≥ 20 or num_comments ≥ 10`, окно по created_utc, `reddit_fullname = data["name"]`, `_permalink = data["permalink"]`, `url = "https://www.reddit.com" + permalink`); HTTP 403 → `_REDDIT_JSON_DISABLED = True`, стадия возвращает `[]`. **Список сабов берём из `news_pipeline.REDDIT_SUBREDDITS`.**
- Слияние → `dedup_cross_source` → отбрасывание нерелевантных: `_score = notifier.score_article(a)` + keyword-фильтр `notifier._kw_matches(title, KEYWORDS) or _kw_matches(summary, KEYWORDS)`. (Стем-вариант: `tools_digest._stem_matches` к ним же — раскомментировать не надо, использовать как в `tools_digest._tool_score`.) Сортировка по score desc, кап: 8 HN + 8 Reddit (≤ 16 всего).

- [ ] **Step 4: прогнать тест — фиксация**

Run: `pytest tests/test_pain_digest.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add pain_digest.py tests/test_pain_digest.py
git commit -m "Pain: collect HN+Reddit discussions with keyword filter and caps

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 2: Чтение комментариев — `fetch_hn_comments`, `fetch_reddit_comments`

**Files:**
- Modify: `pain_digest.py`
- Test: `tests/test_pain_digest.py`

**Interfaces:**
- Consumes: обсуждение из Task 1 (обязательны `_story.kids` / `_permalink`; при их отсутствии — `[]`).
- Produces: `fetch_hn_comments(disc: dict, max_comments: int = 40) -> list[dict]`, `fetch_reddit_comments(disc: dict, max_comments: int = 30) -> list[dict]` — каждый комментарий `{"text": str, "comment_url": str, "depth": int, "source": "HN"|"Reddit", "discussion_url": str}`; `attach_comments(discussions: list[dict]) -> list[dict]` (добавляет `disc["_comments"]`, HN параллельно 8 потоков, Reddit последовательно с 2с паузой).

- [ ] **Step 1: тесты — BFS/капы/deleted/депс, Reddit-JSON, деградация комментариев**

```python
def test_fetch_hn_comments_caps_and_skips_deleted():
    # 12 веток, дети до 2 уровня, deleted пропускаются, кап 40
def test_fetch_reddit_comments_parses_json_and_403_disables():
    # comments/<id>.json: [post_listing, comment_listing]; 403 → []
```

- [ ] **Step 2: прогнать тест — фиксация**

Run: `pytest tests/test_pain_digest.py -v`
Expected: FAIL (нет функций)

- [ ] **Step 3: реализовать**

- HN: BFS из `disc["_story"]["kids"][:12]` глубиной 2 (второй уровень — до 2 детей на ветку), каждый item через `_hn_get_json(f"item/{id}.json")`, `deleted`/`dead`/пустой `text` — пропуск; `comment_url = f"https://news.ycombinator.com/item?id={id}"`; HTML-очистка: `strip_html` — unescape `&#x27;`, `&gt;`, `&lt;`, `&quot;`, `&amp;`, `&x2F;`-варианты и `re.sub(r"<[^>]+>", " ", ...)` (как в `tools_digest`), итог `[:500]`.
- Reddit: `GET https://www.reddit.com{disc["_permalink"]}.json` (UA `_BROWSER_UA`); ответ `[listing_post, listing_comments]`; обход верхнего уровня `data[0].data.children[1:]` (0-й — сам пост) + один уровень вложенных `data.replies` (guard если replies строка типа `""`); тело `body`, `comment_url = "https://www.reddit.com" + permalink_child`, `[:500]`; 403/таймаут → `[]` и логирование раз в прогон (не в каждый запрос).
- `attach_comments`: HN-дискуссии — `ThreadPoolExecutor(max_workers=8)`; Reddit — последовательно, `time.sleep(2)` между обсуждениями; каждый fetch в try/except.

- [ ] **Step 4: прогнать тест — фиксация**

Run: `pytest tests/test_pain_digest.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add pain_digest.py tests/test_pain_digest.py
git commit -m "Pain: HN/Reddit comment fetchers with caps and degradation

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 3: Фрагменты — `extract_fragments`

**Files:**
- Modify: `pain_digest.py`
- Test: `tests/test_pain_digest.py`

**Interfaces:**
- Consumes: обсуждения с `_comments` (Task 2).
- Produces: `extract_fragments(discussions: list[dict]) -> list[dict]`; фрагмент `{"text", "source", "discussion_url", "discussion_title", "comment_url", "kind": "comment"|"post"}`; константы `FRAGMENT_CAP = 50`, `FRAGMENT_TEXT_MAX = 400`.

- [ ] **Step 1: тест — маркеры, самопосты, кап**

```python
def test_extract_fragments_markers_posts_and_cap():
    # комментарий с "is there a way" → фрагмент kind=comment
    # самопост Ask HN "Ask HN: how do I ..." → фрагмент kind=post (всегда passes)
    # комментарий без маркеров → не попадает
    # суммарно > 50 → обрезано до 50, по порядку обсуждений
```

- [ ] **Step 2: прогнать тест — фиксация**

Run: `pytest tests/test_pain_digest.py -v`
Expected: FAIL

- [ ] **Step 3: реализовать**

По обсуждению в порядке списка: сначала selftext-фрагмент — если `summary` непустой (у HN это `story.text`, у Reddit — тело поста из new.json), kind=post; затем комментарии через `_PAIN_MARKERS.search(text)` (регекс из спека §3: `\?`, `how do (you|i)`, `does anyone`, `any(one|body) (else )?know`, `is there a way`, `can('t|not|'t get)`, `stuck|struggling|confus`, `frustrat|annoying|problem is`, `the hard part|pain point|regression`, `wasted (hours|days)`, `wish.*handled`, `what do you (use|recommend)`, `recommendat` — флаг `re.I`); post-фрагменты идут без маркер-проверки (сам факт поста-вопроса или поста-болячки фильтрует keyword-слой). Отсечение до 50 по порядку. `_PAIN_MARKERS` как константа модуля.

- [ ] **Step 4: прогнать тест — фиксация**

Run: `pytest tests/test_pain_digest.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add pain_digest.py tests/test_pain_digest.py
git commit -m "Pain: extract question/pain fragments (regex filter, cap 50)

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 4: AI-анализ — `analyze_fragments` (+ фолбэк)

**Files:**
- Modify: `pain_digest.py`
- Test: `tests/test_pain_digest.py`

**Interfaces:**
- Consumes: фрагменты Task 3; `client.chat.completions.create(...)` (как в `tools_digest.select_tools`, модель `openai/gpt-oss-120b`).
- Produces: `analyze_fragments(fragments: list[dict], client=None) -> dict` — ключи результата: `cards: list[dict]`, `notes: str`, `mode: "ai"|"fallback"`; карточка `{"id": str (p<N> — присваивает Task 6), "problem", "category", "essence", "hook", "content_angle", "solution_status", "potential", "evidence": [{"source", "discussion_url", "comment_url", "quote"}], "evidence_count": int, "single": bool}`; `PAIN_CATEGORIES: tuple[str, ...]`. (`set_pain_status` лежит в Task 5, см. его Interfaces.)

- [ ] **Step 1: тесты — провенанс, нормализация, пересчёт, кап, фолбэк**

```python
def test_analyze_provenance_fabricated_quote_dropped():
    # цитата не является подстрокой переданного фрагмента → карточка выброшена
def test_analyze_normalizes_unknown_category_and_recounts_evidence():
    # category="квант" → "другое"; solution_status="мб" → "не найдено";
    # evidence_count = уникальные discussion_url (не число модели)
def test_analyze_caps_six_cards_and_fallback_without_client():
    # cards больше 6 → обрезка; client=None → mode=fallback, cards пуст
```

- [ ] **Step 2: прогнать тест — фиксация**

Run: `pytest tests/test_pain_digest.py -v`
Expected: FAIL

- [ ] **Step 3: реализовать**

- `_PAIN_SYSTEM_RULES`: правила спека §3 (запрет выдумывать проблемы/цитаты; цитаты — дословные обрезки из входа; суть по-русски; группировка одинаковых проблем из разных обсуждений; `_NATURAL_TEXT_RULES`).
- `_PAIN_PROMPT`: вход-фрагменты (`- source | discussion_title\n  url: ... | comment_url: ...\n  текст: ...[:400]`), выход-JSON по схеме спека (`cards`, `notes`).
- `_parse_pain_json(raw) -> Optional[dict]` — копия `_parse_tools_json` с guard `isinstance(parsed.get("cards"), list)`.
- `_normalize_card(c, frag_index) -> Optional[dict]`: все поля к строке; `category` из `PAIN_CATEGORIES` иначе «другое»; `solution_status` ∈ {«подтверждено», «предложено», «не найдено»} иначе «не найдено»; провенанс: каждый `evidence`-элемент — `comment_url` в переданном наборе И `quote` (normalize whitespace) — подстрока текста фрагмента; карточка с хотя бы одним невалидным evidence отбрасывается (карточке не проходит ни одна цитата под вердикт wrong — по образцу `tools_digest`); `evidence_count = len({e["discussion_url"]})`, `single = evidence_count == 1`; один и тот же фрагмент может стоять доказательством в нескольких карточках; кап 6 карточек.
- Без client или пустые фрагменты → `_fallback_cards(fragments)`: до 10 фрагментов списком, `mode="fallback"`, `cards=[]`.

- [ ] **Step 4: прогнать тест — фиксация**

Run: `pytest tests/test_pain_digest.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add pain_digest.py tests/test_pain_digest.py
git commit -m "Pain: one Groq call, provenance check, card normalization, fallback

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 5: Рендер — `build_pain_messages` + статусы

**Files:**
- Modify: `pain_digest.py`
- Test: `tests/test_pain_digest.py`

**Interfaces:**
- Consumes: результат Task 4; `editorial._split_by_paragraphs(text, limit=4000) -> list[str]` (сущ.); статусы из `bot_data["pain_status"]` (Task 6).
- Produces: `render_pain_cards(result) -> str`, `render_pain_notes(result) -> str`, `build_pain_messages(result) -> list[str]`; статус-словарь: `STATUS_VOCAB = {"new", "taken", "posted", "dropped"}`, `set_pain_status(store: dict, number: int, status: str) -> Optional[str]` (значение — карточка обновлена, `None`; иначе строка-ошибка для `/pain_set`).

- [ ] **Step 1: тест — блок карточки, хук, proof-ссылки, статусы, 4096**

```python
def test_render_pain_cards_format_and_status():
    # "🎯 <b>1. <problem></b>" + категория·angle·обсуждений: N + пометка «пока единичное»
    # решение не найдено → пометка «решения нет» ; иначе — solution_status
    # evidence ≥ 2 → в списке максимум 3 ссылки; status posted → «Статус: опубликована»
def test_build_pain_messages_limits_and_escape():
    # html-инъекция в problem/quote/hook → экранирована; ≤ 4096 на блок
def test_set_pain_status_validates_number_and_vocab():
    # невалидный номер → строка-ошибка; unknown status → ошибка; валидный → None
```

- [ ] **Step 2: прогнать тест — фиксация**

Run: `pytest tests/test_pain_digest.py -v`
Expected: FAIL

- [ ] **Step 3: реализовать**

Формат карточки (§5 спеки), всё через `html.escape`; статус-подписи: `new` → нет подписи, `taken` → «взята в работу», `posted` → «опубликована», `dropped` → «отклонена»; блок-примечания: `notes`, статус каждой карточки; `mode=="fallback"` — явная приписка из spec §6. `set_pain_status`: валидация по `pain_display_map` и `STATUS_VOCAB`.

- [ ] **Step 4: прогнать тест — фиксация**

Run: `pytest tests/test_pain_digest.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add pain_digest.py tests/test_pain_digest.py
git commit -m "Pain: render cards/notes blocks and status commands

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 6: Кэш и seen — `get_or_build_pain`

**Files:**
- Modify: `pain_digest.py`
- Test: `tests/test_pain_digest.py`

**Interfaces:**
- Consumes: Tasks 1–5; `news_pipeline.normalize_url`.
- Produces: `get_or_build_pain(store: dict, hours: int, client=None) -> list[str]`; `bot_data`-ключи `pain_cache` ({"hours", "result"}), `pain_seen: set`, `pain_status: dict`, `pain_counter: int`, `pain_display_map: dict[int, str]` — потребляет Task 7.

- [ ] **Step 1: тест — кэш, окно, seen, счётчик id**

```python
def test_get_or_build_pain_cache_seen_and_ids():
    # 1-й прогон: collect_called=1; pain_seen заполнен ВСЕМИ fragment discussion_url
    # повторный вызов тем же окном → кэш, AI не вызывается
    # новое окно → пересбор; уже seen-обсуждения отфильтрованы
    # ids присваиваются по счётчику: p1, p2 … и не переиспользуются между прогонами
    # pain_display_map: {1: id} от последнего показа
```

- [ ] **Step 2: прогнать тест — фиксация**

Run: `pytest tests/test_pain_digest.py -v`
Expected: FAIL

- [ ] **Step 3: реализовать**

Как `get_or_build_tools`, но: (a) seen — по ВСЕМ фрагментам, не только попавшим в карточки (правило spec §4); (b) перед отрисовкой — присвоение постоянных id (`store["pain_counter"]`), map «номер последнего показа → id» в `pain_display_map`, статус инжектится в карточку из `pain_status` перед рендером; (c) 404/пустой сбор — строка «проблем аудитории за окно не нашлось — попробуйте /pain 168» (spec §5).

- [ ] **Step 4: прогнать тест — фиксация**

Run: `pytest tests/test_pain_digest.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add pain_digest.py tests/test_pain_digest.py
git commit -m "Pain: get_or_build_pain with cache, seen, permanent card ids

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 7: Бот — `cmd_pain`, `cmd_pain_set`, регистрация

**Files:**
- Modify: `bot.py` (импорт, `cmd_pain` после `cmd_tools`, `cmd_pain_set` после, регистрации `app.add_handler`, `post_init` BotCommand)
- Test: `tests/test_pain_digest.py` (интеграционные части — кэш/seen покрывает Task 6; здесь только формат статуса)

**Interfaces:**
- Consumes: `pain_digest.get_or_build_pain(store, hours, client)`, `pain_digest.set_pain_status(store, number, status)`, `bot._groq_client()` (сущ.), паттерн reply_html + plain-text-фолбэк из `cmd_tools`.
- Produces: команды `/pain`, `/pain_set`.

- [ ] **Step 1: реализации (без тестов — только тонкая упаковка)**

`cmd_pain(update, context)`: clamp `hours` 6–168, default 48; статусная строка «ищу проблемы в обсуждениях… это 1–3 минуты»; `try/except` → «Не получилось собрать дайджест: {e}»; отправка блоков `reply_html` с plain-фолбэком (копия `cmd_tools`).
`cmd_pain_set(update, context)`: `args` — номер + статус; `pain_digest.set_pain_status(...)`; ответ бота — «карточка N: <статус>» или ошибка-строка от функции.
Регистрация: `CommandHandler("pain", cmd_pain)`, `CommandHandler("pain_set", cmd_pain_set)`, `BotCommand("pain", "🎯 Проблемы аудитории из обсуждений: /pain")`.

- [ ] **Step 2: импорт-фиксация**

Run: `python3 -c "import bot; print('ok')"`
Expected: `ok`

Run: `pytest tests/test_pain_digest.py tests/test_bot.py -v`
Expected: PASS (все)

- [ ] **Step 3: Commit**

```bash
git add bot.py
git commit -m "Bot: /pain and /pain_set commands with cache and statuses

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 8: Документация и деплой

**Files:**
- Modify: `CLAUDE.md` (новый раздел «Дайджест проблем аудитории (`pain_digest.py`)» по образцу раздела про `tools_digest.py`)
- Modify: (ничего в проде — только deploys)

- [ ] **Step 1: обновить CLAUDE.md** — раздел по образцу `/tools`: цель, источники (HN Firebase topstories+askstories; Reddit JSON; Algolia не используется), конвейер, капы, форматы карточки, `/pain_set`-статусы, фолбэк.

- [ ] **Step 2: полный тест-прогон**

Run: `pytest tests/ -v`
Expected: PASS (все)

- [ ] **Step 3: пуш и деплой на сервер**

```bash
git push
scp pain_digest.py bot.py news_pipeline.py CLAUDE.md kate_dmitr@193.124.80.196:/opt/antispam_bot/news_parser/
```

- [ ] **Step 4: проверка Reddit JSON с сервера + перезапуск бота**

```bash
ssh kate_dmitr@193.124.80.196 "cd /opt/antispam_bot/news_parser && python3 -c \"import urllib.request,json; print(json.load(urllib.request.urlopen('https://www.reddit.com/r/LocalLLaMA/new.json?limit=1', timeout=15))['kind'])\""
ssh kate_dmitr@193.124.80.196 "cd /opt/antispam_bot/news_parser && ps aux | grep 'python bot.py' | grep -v grep"   # PID
ssh kate_dmitr@193.124.80.196 "kill <PID> && cd /opt/antispam_bot/news_parser && nohup ./venv/bin/python bot.py > bot.log 2>&1 &"
```
Expected: Reddit JSON живёт (`kind` = `Listing`); бот перезапущен, один экземпляр.

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md
git commit -m "Docs: /pain digest

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

## Неопределённое, оставленное имплементёру

- Точные тексты сообщений статуса («Не получилось…», «Собираю…») — по образцу существующих команд бота.
- Формат логирования (print в stdout / logging) — как в файлах-соседях.
- Комментарии кода — только там, где неочевидно, по стилю соседей.