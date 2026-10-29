# Дайджест «ИИ-скаут» — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** команда `/scout` в боте — подборка из до 10 необычных/вирусных ИИ-историй со своим скорингом (5 критериев × 0–2, max 10), карточкой по шаблону инструкции и фактчекингом.

**Architecture:** новый pipeline-модуль `scout.py` (по образцу `tools_digest.py`/`editorial.py`, без Telegram-зависимостей) + один хендлер в `bot.py`. Переиспользуем: сборщики `news_pipeline`, дедуп, полнотекстовую экстракцию, Google-News-корроборацию, `_split_by_paragraphs`, `parse_int_arg`/`resolve_topic`/`filter_unseen` из `editorial.py`.

**Tech Stack:** Python 3, feedparser/urllib (уже в requirements.txt), Groq `openai/gpt-oss-120b` (`reasoning_effort="low"`), pytest.

**Spec:** `docs/superpowers/specs/2026-10-29-scout-design.md` — первичен; план дополняет его точными именами и контрактами.

## Global Constraints

- Модель везде `MODEL = "openai/gpt-oss-120b"`, `reasoning_effort="low"`, `max_tokens` 2000–3000.
- Новых зависимостей нет; все источники keyless; HTTP с UA из `news_pipeline._BROWSER_UA`; каждый сборщик никогда не роняет прогон (fetch-функции возвращают `[]`/`(posts, ok)` при ошибке).
- `total` скоринга **всегда** пересчитывается кодом из критериев; число модели игнорируется.
- Пороги: `PRIORITY_TOTAL = 8`, `LIST_TOTAL = 6`, `FALLBACK_TOTAL = 1` (AI-режим: ≥ LIST_TOTAL, keyword-фолбэк статьи: ≥ FALLBACK_TOTAL); кап кандидатов `CANDIDATE_CAP = 20`; максимум тем в списке `SCOUT_LIST_MAX = 10`; дефолт окна `48` ч, аргумент клампится в 2–168.
- Рубрики — `SCOUT_CATEGORIES` (раздел «Интерфейсы» ниже); `category == "пустышка"` → кандидата отбросить.
- Весь текст от модели через `clean_text` (из `notifier`); все поля карточки с дефолтами при парсинге.
- Весь вывод Telegram — HTML, `html.escape` всего контента (в т.ч. url с `quote=True`), сообщения ≤ 4096 (`editorial._split_by_paragraphs`), фолбэк на plain-text при отказе Telegram.
- Недатированные записи (HF trending, GitHub Trending, HF Papers, arXiv без даты) — показывать с пометкой «трендинг, дата выхода не известна», не выбрасывать.
- `/scout` не автоотправка: `TELEGRAM_AUTO_SEND` не проверяется; ничего не постится в канал.
- `monitor.py` и `notifier.py` не изменяются вообще.
- Кэш карточки — в `topic["card"]` (повторный `/scout N` без нового AI-вызова); seen — `bot_data["scout_seen"]`, список — `bot_data["scout_topics"]`.

## Review Focus

Входы, которые скорее всего больно укусят редактора, и тест, закрепляющий каждый:

1. **Модель вернула мусор/выходящие за 0–2 критерии** — скорер обязан вернуть `None` (фолбэк keyword), total пересчитан; список не пустеет из-за одной статьи. Тест в Task 2.
2. **Недатированный тренд взял верхний балл** — в списке и карточке обязан быть значок «без даты». Тесты в Task 3 и Task 6.
3. **Инъекция HTML через заголовок/URL фида** — экранирование в списке и карточке. Тесты в Task 3 и Task 6.
4. **Fact-чекер пометил claim `wrong`** — утверждение исчезает из `what_happened` и попадает только в rewrite от фактчекера. Тест в Task 5.
5. **`/scout 3` при пустом списке** — нет `IndexError`, число трактуется как окно часов. Тест в Task 7.

---

### Task 1: Сбор кандидатов (`collect_scout_topics`, `fetch_hf_papers`)

**Files:**
- Create: `scout.py`
- Test: `tests/test_scout.py`

**Interfaces:**
- Consumes: `news_pipeline.fetch_rss_feed/fetch_hackernews/fetch_github_trending/fetch_hf_trending/fetch_reddit/fetch_producthunt/dedup_cross_source/_BROWSER_UA`; `notifier.KEYWORDS/_kw_matches/score_article`; `editorial.EDITORIAL_FEEDS`; `tools_digest._stem_matches` (переиспользуем, не копируем).
- Produces:

```python
MODEL = "openai/gpt-oss-120b"
DEFAULT_HOURS = 48
CANDIDATE_CAP = 20
SCOUT_LIST_MAX = 10
PRIORITY_TOTAL, LIST_TOTAL, FALLBACK_TOTAL = 8, 6, 1

SCOUT_FEEDS = [  # 5 новых лент, имена как в таблице спеки §5
    {"name": "The Verge AI", "url": "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"},
    {"name": "TechCrunch AI", "url": "https://techcrunch.com/category/artificial-intelligence/feed/"},
    {"name": "IEEE Spectrum AI", "url": "https://spectrum.ieee.org/feeds/topic/artificial-intelligence.rss"},
    {"name": "MIT Technology Review AI", "url": "https://www.technologyreview.com/topic/artificial-intelligence/feed"},
    {"name": "arXiv cs.RO", "url": "https://export.arxiv.org/rss/cs.RO"},
]
# Если при живой проверке URL что-то не отвечает/отдаёт мусор — поправить URL
# на работающий аналог (альтернативы в спеке §5) и отметить в отчёте.

SCOUT_CATEGORIES = {"А": "Роботы и эксперименты", "Б": "Необычное применение",
                    "В": "Провалы и курьёзы", "Г": "Инструменты и агенты",
                    "Д": "Будущее профессий", "Е": "Деньги индустрии"}
SCOUT_REJECT_CATEGORY = "пустышка"

SCOUT_KEYWORDS = KEYWORDS + [  # rubric слова к базовым KEYWORDS из notifier
    "robot", "робот", "humanoid", "drone", "демо", "demo", "prototype",
    "прототип", "experiment", "эксперимент", "research", "исследован",
    "study", "fail", "провал", "backfire", "hallucinat", "галлюцинац",
    "jobs", "професси", "billion", "стоимость", "расходы", "выручка",
    "startup", "видео", "video", "дизайн-робот",
]
SCOUT_KEYWORD_WEIGHTS = {"robot": 3, "робот": 3, "humanoid": 3, "demo": 2,
                         "демо": 2, "fail": 2, "провал": 2, "study": 2,
                         "исследован": 2, "hallucinat": 2, "billion": 2}

def _scout_score(article: dict) -> int: ...
    # score_article(article) + сумма весов стемов SCOUT_KEYWORDS
    # (tools_digest._stem_matches по title+summary в lower())
def fetch_hf_papers() -> list[dict]: ...
    # GET https://huggingface.co/papers с UA, regex по href="/papers/<id>",
    # статьи {"title", "url": f"https://huggingface.co/papers/<id>",
    # "summary": title, "source": "HF Papers", "published": None}; ≤ 20 шт;
    # ЛЮБАЯ ошибка → [] (never raises)
def collect_scout_topics(hours: int = DEFAULT_HOURS) -> list[dict]: ...
    # SCOUT_FEEDS + editorial.EDITORIAL_FEEDS (fetch_rss_feed) + fetch_hf_papers()
    # + news_pipeline.fetch_hackernews(hours) + fetch_github_trending(hours)
    # + fetch_hf_trending() + fetch_reddit(subreddits=REDDIT_SUBREDDITS + ["artificial"], hours)
    # + fetch_producthunt(hours) → dedup_cross_source → фильтр _scout_score(a) > 0
    # → сортировка по _scout_score desc → [:CANDIDATE_CAP]. Never raises.
```

- [ ] **Step 1: тесты сбора** — код тестов в стиле `test_editorial.py` (тот же `_fake_client` не нужен; нужен `_article`-хелпер с `published`):

```python
def _article(title="Claude научился ставить скилы сам", url="https://a.example/1",
             summary="Anthropic skills agent", **extra):
    a = {"title": title, "url": url, "summary": summary,
         "source": "Test", "published": None}
    a.update(extra)
    return a

def test_collect_scout_topics_merges_all_sources_dedups_and_caps():
    # patch всеnews_pipeline-сборщики на []/фейки + scout.fetch_hf_papers на [];
    # fetch_rss_feed возвращает для каждого имени по 1 статье с одним url
    # → после дедупа 1 статья (10 фидов), " + " in source

def test_collect_scout_topics_drops_non_scout_articles():
    # борщ без ни одного слова из SCOUT_KEYWORDS не проходит;
    # статья с "robot learned to" и без AI-слов — проходит (рубричный фильтр шире KEYWORDS)

def test_collect_scout_topics_caps_at_20_and_orders_by_score():
    # 25 одинаковых статей → len == 20; сильная ("robot humanoid demo") впереди слабой

def test_fetch_hf_papers_parses_and_never_raises():
    # patch urllib.request.urlopen на fixture-HTML с двумя /papers/2510.01010 → 2 статьи, url и title корректны
    # patch на Exception → []
```

- [ ] **Step 2:** `pytest tests/test_scout.py -v` → FAIL (`No module named scout`).
- [ ] **Step 3:** реализовать `scout.py`: константы, `_scout_score`, `fetch_hf_papers`, `collect_scout_topics` (тела — по контрактам выше; `fetch_hf_papers` — `urllib.request.Request` + `re.findall`).
- [ ] **Step 4:** `pytest tests/test_scout.py -v` → PASS.
- [ ] **Step 5:** Commit: `git commit -m "Scout: candidate collection from 10 feeds + HF Papers"` (+ Co-Authored-By line).

### Task 2: Скоринг (`score_scout_topic_ai`, `select_scout_topics`)

**Files:**
- Modify: `scout.py`
- Test: `tests/test_scout.py`

**Interfaces:**
- Consumes: `editorial._extract_json_object`, `news_pipeline.extract_full_text`, `notifier.score_article/clean_text/_NATURAL_TEXT_RULES`.
- Produces:

```python
SCOUT_CRITERIA = ["неожиданность", "наглядность", "понятность",
                  "релевантность", "потенциал обсуждения"]

def _parse_scout_score(raw) -> dict | None: ...
    # через editorial._extract_json_object; criteria 0–2 каждый (bool → None);
    # category ∈ SCOUT_CATEGORIES или == SCOUT_REJECT_CATEGORY (тот → None);
    # one_liner через clean_text; total = sum(criteria) — ЧИСЛО МОДЕЛИ игнорируется
    # → {"criteria": …, "total": …, "category": "А…Е", "one_liner": str} | None

def score_scout_topic_ai(article, groq_client, full_text=None) -> dict | None: ...
    # 1 вызов модели: промпт перечисляет 5 критериев со смыслом из спеки §1 и
    # правила инструкции: обычный анонс модели без особенности → пустышка;
    # провалы не придумывать; факты отделять от мнений; смысл сумм уточнять.
    # content = full_text or summary, [:3000]. Любая ошибка → None.

def select_scout_topics(articles, groq_client=None) -> tuple[list[dict], bool]: ...
    # точная копия логики editorial.select_topics, но: score_topic_ai →
    # score_scout_topic_ai, _topic_sort_key тот же (total, published),
    # пороги LIST_TOTAL/FALLBACK_TOTAL, топ SCOUT_LIST_MAX.
    # topic dict: {"article": a, "criteria": …, "total": …, "category": …,
    #              "one_liner": …, "ai": bool}
```

- [ ] **Step 1: тесты скоринга**:

```python
_SCORE_RAW = ('{"criteria": {"неожиданность": 2, "наглядность": 2, "понятность": 1, '
              '"релевантность": 2, "потенциал обсуждения": 1}, "total": 9, '
              '"category": "А", "one_liner": "Робот сам собрал кубик"}')

def test_score_parses_valid_json():          # total == 9, criteria["неожиданность"] == 2, category == "А"
def test_score_recomputes_total():            # тот же raw с "total": 55 → total == 8 (сумма 2+2+1+2+1)
def test_score_rejects_out_of_range():        # неожиданность=3 → None
def test_score_rejects_unknown_category():    # category="Ж" → None
def test_score_drops_pustishka():             # category="пустышка" → None
def test_score_none_on_garbage_client_none(): # мусор / RuntimeError-клиент / None → None

def test_select_scout_keeps_ge_6_and_sorts(): # strong(10)/weak(3)/strong → 2 темы, used_ai True, sorted desc
def test_select_scout_fallback_without_client(): # None-клиент → used_ai False, ai=False, порог FALLBACK_TOTAL, топ 10
def test_select_scout_per_article_fallback():  # create бросает RuntimeError → ai=False topic остаётся при used_ai=True
```

- [ ] **Step 2:** pytest → FAIL (функций нет).
- [ ] **Step 3:** реализовать `_parse_scout_score`, `score_scout_topic_ai`, `select_scout_topics` (структура — по `editorial.py:104-208`).
- [ ] **Step 4:** pytest → PASS.
- [ ] **Step 5:** Commit (`Scout: 5-criteria scoring and selection`).

### Task 3: Список (`render_scout_list`)

**Files:**
- Modify: `scout.py`
- Test: `tests/test_scout.py`

**Interfaces:**
- Consumes: `html`, `editorial._fmt_date`, константы Task 1.
- Produces:

```python
def render_scout_list(topics: list[dict], hours: int, used_ai: bool) -> str: ...
    # Заголовок «🔎 Похоже на скам/…» НЕТ: f"🔎 <b>Необычное — подборка скаута</b> ({hours} ч)"
    # пустой список → "Ничего необычного не нашлось. Попробуй шире: /scout 168"
    # used_ai False → warning «режим без AI»
    # строка на тему: "<b>{i}. [{total}/10 · {SCOUT_CATEGORIES[category]} · приоритет|резерв]</b> {one_liner}"
    # + "   ↳ {source} · /scout {i}" + кликабельный <a href>; html.escape ВСЕГО.
    # total >= PRIORITY_TOTAL → "приоритет", иначе "резерв" (в AI-режиме);
    # недатированная статья (published is None) → в той же строке источника
    # « · триндинг, дата выхода не известна»... точный текст: "трендинг на момент сбора, без даты выхода"
    # футер: "Карточка: /scout 1 … /scout {len}"
```

- [ ] **Step 1: тесты списка**:

```python
def test_render_scout_list_numbering_scores_and_links():  # "1.", "/scout 1", "9/10", "приоритет", href на url
def test_render_scout_list_human_category_names():        # "Роботы и эксперименты" в тексте (не "А")
def test_render_scout_list_escapes_malicious_url():       # url с "><a href=… → экранирован, второй href не инжектился
def test_render_scout_list_dateless_badge():              # published None → "без даты" в тексте
def test_render_scout_list_empty_and_no_ai():             # [] → подсказка окна; used_ai=False → "без AI"
```

- [ ] **Step 2:** pytest → FAIL. **Step 3:** реализовать. **Step 4:** pytest → PASS.
- [ ] **Step 5:** Commit (`Scout: short list rendering`).

### Task 4: Карточка (`build_scout_card`)

**Files:**
- Modify: `scout.py`
- Test: `tests/test_scout.py`

**Interfaces:**
- Consumes: `editorial._CARD_SYSTEM_PROMPT`-стиль (свой промпт), `notifier.clean_text/_NATURAL_TEXT_RULES`, константы Task 1–2.
- Produces:

```python
SCOUT_POST_FORMATS = ("новость с объяснением", "необычный эксперимент",
                      "забавный провал", "практический разбор инструмента",
                      "дискуссионный пост", "короткий видеопост", "опрос аудитории")

def _parse_scout_card_json(raw) -> dict | None: ...
    # поля: hook, what_happened (обязательные), why_interesting, post_format
    # (обязателен, ∈ SCOUT_POST_FORMATS), visual, score_why, claims (≤ 6, чистятся).
    # Возврат {"hook", "what_happened", "why_interesting", "post_format",
    #          "visual", "score_why", "claims"} | None.

def build_scout_card(topic: dict, groq_client=None) -> dict: ...
    # 1 AI-вызов по _full_text[:3500]; промпт: только факты материала,
    # §6 спеки (визуал «происхождение не проверено», если нет подтверждения;
    # заголовок не преувеличивает; не пиши готовый пост), _NATURAL_TEXT_RULES.
    # Без клиента / ошибка → _scout_card_stub(topic):
    #   hook=title, what_happened=summary[:600], post_format="", visual="не найден",
    #   score_why="", claims=[], no_ai=True, first_source_url=url, status="требует проверки",
    #   confirmed=[], needs_check=[], sources=[]
    # При успехе: card = stub + parsed + first_source_url + no_ai=False
    # полные поля карточки: {"article", "total", "category", ...из topic..., 
    #   "hook", "what_happened", "why_interesting", "post_format", "visual",
    #   "score_why", "claims", "confirmed", "needs_check", "sources", "status",
    #   "first_source_url", "published", "no_ai"}

def get_or_build_scout_card(topic, groq_client=None) -> dict: ...
    # как editorial.get_or_build_card: topic["card"] кэш; строит card +
    # fact_check_scout_card(card, article, client) + card["published"] = article.get("published")
    # (фактчекинг вызывает, только если no_ai is False)
```

- [ ] **Step 1: тесты карточки** (raw-JSON в стиле `_CARD_RAW` из `test_editorial.py`):

```python
_CARD_RAW = '''```json
{"hook": "Робот сам собрал кубик Рубика ⚡", 
 "what_happened": "Команда X показала четвероногого робота, который сам собрал кубик. Демо снято на видео 28.10.",
 "why_interesting": "Такое раньше делали только промышленные манипуляторы.",
 "post_format": "необычный эксперимент", "visual": "видео демо в статье",
 "score_why": "высокая неожиданность и наглядность", 
 "claims": ["Robot собрал кубик Рубика", "Видео от 28.10"]} 
```'''

def test_build_scout_card_parses_full_json():   # все поля на месте, no_ai False
def test_build_scout_card_rejects_bad_format(): # post_format="статья" → вернулся stub (no_ai True)
def test_build_scout_card_stub_without_client():# hook == article title, status "требует проверки"
def test_get_or_build_scout_card_caches():      # второй вызов с client=None вернул тот же объект
```

- [ ] **Step 2:** pytest → FAIL. **Step 3:** реализовать.
- [ ] **Step 4:** pytest → PASS. **Step 5:** Commit (`Scout: card builder per §7 template`).

### Task 5: Фактчекинг карточки (`fact_check_scout_card`, `_verification_status`)

**Files:**
- Modify: `scout.py`
- Test: `tests/test_scout.py`

**Interfaces:**
- Consumes: `notifier._parse_factcheck_json/_publishers_from_google_news`, `news_pipeline.fetch_google_news/normalize_url`, `editorial._extract_json_object`.
- Produces:

```python
def _parse_scout_factcheck(raw) -> dict | None: ...
    # {"verdicts": <результат notifier._parse_factcheck_json(json.dumps(verdicts))>,
    #  "hook": str, "what_happened": str} | None (verdicts не list → None)

def fact_check_scout_card(card: dict, article: dict, groq_client=None) -> dict: ...
    # механика editorial.fact_check_card (editorial.py:349-437) с заменами:
    # промпт сверяет claims с текстом статьи; rewrite-поля: "hook" и
    # "what_happened" (вместо headline/body/cta);
    # wrong-утверждение исчезает из confirmed/needs_check; uncertain —
    # корроборация fetch_google_news([query]) ≥ 2 издателей → префикс
    # "✓ (подтверждено другими СМИ): ", ссылки в card["sources"] (≤ 3, свой url исключён)
    # без клиента/ошибки/пустых verdicts: confirmed=[], needs_check=claims, тексты не меняем
    # В КОНЦЕ всегда: card["status"] = _verification_status(card) — так Task 6
    # и get_or_build_scout_card (Task 4) получают готовый статус из карточки.

def _verification_status(card: dict) -> str: ...
    # "подтверждено" — все claims ok; "частично подтверждено" — есть
    # подтверждённые + хотя бы один uncertain с корроборацией;
    # иначе (есть несверенный/uncertain без корроборации) → "требует проверки"
    # card без факчекинга (confirmed/needs_check пусты, claims пусты) → "требует проверки"
```

- [ ] **Step 1: тесты фактчекинга** (по образцу тестов `fact_check_card` в `test_editorial.py`, raw:

```python
_FC_RAW = ('{"verdicts": ['
           '{"n": 1, "verdict": "ok", "text": "Robot собрал кубик Рубика", "query": "robot rubiks cube"}, '
           '{"n": 2, "verdict": "uncertain", "text": "Видео от 28.10", "query": "robot rubiks video"}], '
           '"hook": "", "what_happened": "Команда X показала робота, собравшего кубик."}')
```

```python
def test_fact_check_scout_card_splits_verdicts_and_rewrites():  # confirmed=[текст1], needs_check=[текст2], what_happened переписан, hook пуст → не тронут
def test_fact_check_scout_card_without_client():                # needs_check == claims, тексты не изменены
def test_fact_check_scout_card_wrong_claim_dropped():           # verdict wrong — утверждение ни в confirmed, ни в needs_check
def test_fact_check_scout_card_corroboration_and_sources():     # 2 разных издателя → "✓ (подтверждено другими СМИ)" + 2 sources, свой url исключён
def test_verification_status_labels():                          # all ok → "подтверждено"; ok+uncertain(corrob) → "частично подтверждено"; uncertain без corrob → "требует проверки"
```

- [ ] **Step 2:** pytest → FAIL. **Step 3:** реализовать.
- [ ] **Step 4:** pytest → PASS. **Step 5:** Commit (`Scout: card fact-check with corroboration and status`).

### Task 6: Рендер карточки (`render_scout_card_messages`)

**Files:**
- Modify: `scout.py`
- Test: `tests/test_scout.py`

**Interfaces:**
- Consumes: `editorial._split_by_paragraphs`, `_fmt_date`, `html`, константы/функции Tasks 1–5.
- Produces:

```python
def render_scout_card_messages(topic: dict, card: dict) -> list[str]: ...
    # ≤ 4096 каждое, порядок полей по шаблону §7 инструкции:
    # 1. header: "🗂 <b>{hook}</b>" + "Категория: {label} · Оценка: {total}/10 · {status}" 
    #    + дата строки editorial._fmt_date(publised)
    # 2. "Что произошло:", "Почему интересно:", "Потенциал поста: {post_format}",
    #    "Визуал:", "Источники:" (первоисточник <a href> + card["sources"]),
    #    "Статус проверки: {status}", "Оценка: {score_why}"
    # confirmed → буллеты, needs_check → буллеты (уже с "✓ (подтверждено…)")
    # ВСЁ контентное через html.escape, url через html.escape(quote=True)
```

- [ ] **Step 1: тесты рендера**:

```python
def test_render_scout_card_all_fields_present():  # hook, what_happened, score_why, post_format в первом сообщении
def test_render_scout_card_escapes_html():        # "<script>" в what_happened → &lt;script&gt;, len(m) <= 4096 для всех
def test_render_scout_card_status_and_sources():  # "подтверждено", href на first_source_url и на card["sources"][0]
def test_render_scout_card_dateless():            # published None → "трендинг на момент сбора"
```

- [ ] **Step 2:** pytest → FAIL. **Step 3:** реализовать.
- [ ] **Step 4:** pytest → PASS. **Step 5:** Commit (`Scout: card rendering with split and escape`).

### Task 7: Бот (`/scout`) + документация

**Files:**
- Modify: `bot.py:589-706` (рядом с cmd_digest/cmd_card, до post_init)
- Modify: `bot.py:333` (строка help в cmd_start)
- Modify: `CLAUDE.md` (новая секция «Дайджест ИИ-скаута» по образцу секций `/digest` и `/tools`: назначение, источники, конвейер, режимы деградации)
- Test: `tests/test_bot_scout.py`

**Interfaces:**
- Consumes: все функции `scout.py`, `editorial.parse_int_arg/resolve_topic/filter_unseen/parse_int_arg`, `news_pipeline.normalize_url`.
- Produces:

```python
async def cmd_scout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None: ...
    # n = editorial.parse_int_arg(context.args)
    # topics = context.bot_data.get("scout_topics", [])
    # если topics и n in 1..len(topics) → РЕЖИМ КАРТОЧКИ (см. ниже)
    # иначе → РЕЖИМ СПИСКА: hours = clamp(n или DEFAULT_HOURS, 2, 168)
    #   status-реплай «Ищу необычное за X ч…» → client = _groq_client()
    #   topics, used_ai = scout.select_scout_topics(scout.collect_scout_topics(hours), client)
    #   seen = set(bot_data.get("scout_seen", ())) ; fresh = editorial.filter_unseen(topics, seen)
    #   bot_data["scout_topics"] = fresh ; bot_data["scout_seen"] = seen
    #   status.edit_text(scout.render_scout_list(fresh, hours, used_ai), parse_mode="HTML",
    #                    disable_web_page_preview=True)
    # РЕЖИМ КАРТОЧКИ (n-номер, по образцу cmd_card bot.py:637-665):
    #   topic = editorial.resolve_topic(topics, n)
    #   status «Строю карточку скаута…» → card = scout.get_or_build_scout_card(topic, _groq_client())
    #   scout_seen += normalize_url(topic["article"]["url"]) → сохранение; status.delete()
    #   for msg in scout.render_scout_card_messages(topic, card): reply_html(msg, …)
    #     c try/except → plain-text фолбэк (копия паттерна cmd_card:661-665)
```

- [ ] **Step 1: тесты ботового слоя** (функции команд синхронно не тестируем — только разбор аргументов через вспомогательную чистую функцию; вынеси в scout.py её, чтобы тест не требовал Telegram):

```python
# в scout.py:
def resolve_scout_command(args: list[str], n_topics: int) -> dict: ...
    # {"mode": "hours", "value": 48|clamped-n} или {"mode": "card", "n": int}
    # n is None → hours; topics пуст → n трактуется как hours (кламп 2..168);
    # n in 1..n_topics → card; n > n_topics → hours

def test_resolve_scout_command_rules():
    # ([], 5) → hours 48            (нет аргумента — дефолт)
    # (["3"], 5) → card 3           (есть список, номер валиден)
    # (["3"], 0) → hours 3          (списка нет — число трактуется как окно)
    # (["72"], 5) → hours 72        (больше максимума списка — окно)
    # (["1"], 0) → hours 2          (кламп вниз до минимума)
    # (["abc"], 5) → hours 48       (не число — дефолт)
    # (["11"], 10) → hours 11       (11 > n_topics=10 — окно, не номер)
```

Правило спеки: число `1..n_topics` при **непустом** списке — номер карточки,
**любое другое** число (или пустой список) — окно часов с клампом 2–168.

Внимание: `hours` аргумент клампится ТОЛЬКО снизу (2) и сверху (168); «11» при пустом списке — валидное окно часов, не номер (списка нет).
- [ ] **Step 2:** pytest → FAIL. **Step 3:** реализовать `resolve_scout_command` в `scout.py` + `cmd_scout` в `bot.py`; регистрация `CommandHandler("scout", cmd_scout)` и `BotCommand("scout", "🔎 Необычные ИИ-истории: /scout | /scout 3")` в `post_init`; строка в help `cmd_start`.
- [ ] **Step 4:** pytest → PASS; затем **весь** прогон: `pytest tests/ -v` → все зелёные.
- [ ] **Step 5:** ручная проверка без ключа: `unset GROQ_API_KEY && python3 -c "import scout; t=scout.collect_scout_topics(24); print(len(t))"` — не падает (сбор идёт без AI).
- [ ] **Step 6:** Commit (`Bot: /scout command with card mode and docs`).