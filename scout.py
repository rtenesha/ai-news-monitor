#!/usr/bin/env python3
"""AI-scout digest per the 2026-10-29 editor instruction: unusual, fresh,
potentially viral AI stories — collection, 5-criteria scoring (max 10),
editor cards with fact-check. Spec:
docs/superpowers/specs/2026-10-29-scout-design.md."""

from __future__ import annotations

import html
import json as _json
import re
import urllib.request

import editorial
import news_pipeline
import notifier
import tools_digest
from notifier import KEYWORDS, score_article, clean_text

MODEL = "openai/gpt-oss-120b"

DEFAULT_HOURS = 48
CANDIDATE_CAP = 20
SCOUT_LIST_MAX = 10
PRIORITY_TOTAL = 8  # 8-10 — приоритет
LIST_TOTAL = 6      # 6-7 — резерв; <6 — отброшено (в AI-режиме)
FALLBACK_TOTAL = 1  # порог списка в режиме без AI

SCOUT_FEEDS = [
    {"name": "The Verge AI", "url": "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"},
    {"name": "TechCrunch AI", "url": "https://techcrunch.com/category/artificial-intelligence/feed/"},
    {"name": "IEEE Spectrum AI", "url": "https://spectrum.ieee.org/feeds/topic/artificial-intelligence.rss"},
    {"name": "MIT Technology Review AI", "url": "https://www.technologyreview.com/topic/artificial-intelligence/feed"},
    {"name": "arXiv cs.RO",
     # RSS-обёртки arXiv (export/rss.arxiv.org rss) отдавали stub с 0 записей —
     # проверено вживую 2026-10-29. Официальное Atom API работает и содержит даты.
     "url": "https://export.arxiv.org/api/query?search_query=cat:cs.RO"
            "&sortBy=submittedDate&sortOrder=descending&max_results=15",
     # arXiv публикует с лагом: свежие статьи старше 48ч — окно не уже 96ч
     "min_hours": 96},
]

SCOUT_CATEGORIES = {"А": "Роботы и эксперименты",
                    "Б": "Необычное применение",
                    "В": "Провалы и курьёзы",
                    "Г": "Инструменты и агенты",
                    "Д": "Будущее профессий",
                    "Е": "Деньги индустрии"}
SCOUT_REJECT_CATEGORY = "пустышка"  # сюда попавшее отбрасывается

SCOUT_KEYWORDS = KEYWORDS + [
    "robot", "робот", "humanoid", "drone", "демо", "demo", "prototype",
    "прототип", "experiment", "эксперимент", "research", "исследован",
    "study", "fail", "провал", "backfire", "hallucinat", "галлюцинац",
    "jobs", "професси", "billion", "стоимость", "расходы", "выручка",
    "startup", "видео", "video", "дизайн-робот",
]
SCOUT_KEYWORD_WEIGHTS = {"robot": 3, "робот": 3, "humanoid": 3, "demo": 2,
                         "демо": 2, "fail": 2, "провал": 2, "study": 2,
                         "исследован": 2, "hallucinat": 2, "billion": 2}

_stem_matches = tools_digest._stem_matches


def _scout_score(article: dict) -> int:
    text = (article["title"] + " " + article["summary"]).lower()
    score = score_article(article)
    for kw in SCOUT_KEYWORDS:
        if _stem_matches(kw, text):
            score += SCOUT_KEYWORD_WEIGHTS.get(kw, 1)
    return score


def fetch_hf_papers() -> list[dict]:
    """Front-page papers of huggingface.co/papers (no official RSS). The live
    page (checked 2026-10-29) carries id/title inside HTML-escaped data-props
    JSON blobs — unescape and pull '"paper":{"id":...,"title":...' pairs;
    legacy <a href="/papers/..."> anchors kept as a fallback pattern. Never
    raises — a failed or unparsable page just yields no candidates."""
    url = "https://huggingface.co/papers"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": news_pipeline._BROWSER_UA})
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except Exception:
        return []
    text = html.unescape(raw)
    found = re.findall(
        r'"paper":\{"id":"(\d{4}\.\d{4,6})".{0,2000}?"title":"([^"]+)"', text)
    if not found:
        found = re.findall(
            r'href="/papers/(\d{4}\.\d{4,6})"[^>]*>([^<]+)<', raw)
    papers, seen = [], set()
    for pid, title in found:
        if pid in seen:
            continue
        seen.add(pid)
        papers.append({"title": clean_text(title), "url": f"https://huggingface.co/papers/{pid}",
                       "summary": clean_text(title), "source": "HF Papers",
                       "published": None})
    return papers[:20]


def collect_scout_topics(hours: int = DEFAULT_HOURS) -> list[dict]:
    """Gather candidates from all scout sources: dedup, keyword filter,
    cap by combined keyword score. Never raises (each fetcher is safe)."""
    articles: list[dict] = []
    for feed in SCOUT_FEEDS + editorial.EDITORIAL_FEEDS:
        feed_hours = max(hours, feed.get("min_hours", 0))
        posts, _ok = news_pipeline.fetch_rss_feed(feed["name"], feed["url"], feed_hours)
        articles += posts
    articles += fetch_hf_papers()
    articles += news_pipeline.fetch_hackernews(hours=hours)
    articles += news_pipeline.fetch_github_trending(hours=hours)
    articles += news_pipeline.fetch_hf_trending()
    articles += news_pipeline.fetch_reddit(
        subreddits=news_pipeline.REDDIT_SUBREDDITS + ["artificial"], hours=hours)
    articles += news_pipeline.fetch_producthunt(hours=hours)
    articles = news_pipeline.dedup_cross_source(articles)
    relevant = [a for a in articles if _scout_score(a) > 0]
    return sorted(relevant, key=_scout_score, reverse=True)[:CANDIDATE_CAP]


SCOUT_CRITERIA = ["неожиданность", "наглядность", "понятность",
                  "релевантность", "потенциал обсуждения"]

_CRITERIA_HELP = (
    "- неожиданность: есть ли в истории что-то необычное\n"
    "- наглядность: можно ли показать результат на фото, видео, скриншоте или простой схеме\n"
    "- понятность: получится ли объяснить суть без сложного технического контекста\n"
    "- релевантность: интересна ли аудитории Zerocoder (ИИ и вайбкодинг)\n"
    "- потенциал обсуждения: вызывает ли удивление, спор, улыбку или желание поделиться"
)

_SCORE_SYSTEM_PROMPT = (
    "Ты — редактор-скаут Telegram-канала Zerocoder (ИИ, вайбкодинг, агенты). "
    "Ищешь необычные и потенциально вирусные истории, а не обычные релизы. "
    "Пиши исключительно на русском языке. Отвечай СТРОГО одним валидным "
    "JSON-объектом, без пояснений."
)

_SCOUT_RULES = (
    "- обычный анонс очередной модели без действительно интересной особенности — "
    "пустышка;\n"
    "- провалы и курьёзы не придумывай и не преувеличивай: только реальные случаи "
    "с подтверждением из материала;\n"
    "- отделяй подтверждённые факты от мнений, прогнозов и гипотез;\n"
    "- для сумм денег уточняй смысл: выручка, расходы, инвестиции или оценка."
)


def _parse_scout_score(raw) -> dict | None:
    """Validate scorer JSON: criteria 0-2 each, category from known list.
    total is RECOMPUTED from criteria (model's own number ignored).
    Категория «пустышка» = история не годится → None (отброс)."""
    data = editorial._extract_json_object(raw)
    if not data:
        return None
    given = data.get("criteria")
    if not isinstance(given, dict):
        return None
    criteria = {}
    for key in SCOUT_CRITERIA:
        v = given.get(key)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= int(v) <= 2:
            return None
        criteria[key] = int(v)
    category = str(data.get("category", "")).strip()
    one_liner = clean_text(str(data.get("one_liner", ""))).strip()[:160]
    if category not in SCOUT_CATEGORIES or not one_liner:
        return None
    if category == SCOUT_REJECT_CATEGORY:
        return None
    return {"criteria": criteria, "total": sum(criteria.values()),
            "category": category, "one_liner": one_liner}


def score_scout_topic_ai(article: dict, groq_client, full_text=None) -> dict | None:
    """Scout 5-criteria scoring of one article (max 10). Returns None on any
    failure — caller falls back to keyword scoring for this article."""
    if not groq_client:
        return None
    content = full_text or article["summary"]
    prompt = (
        "Оцени историю по 5 критериям — каждый 0, 1 или 2 "
        "(2 — сильно, 0 — нет):\n\n"
        f"{_CRITERIA_HELP}\n\n"
        "Выбери ОДНУ категорию строго из списка: "
        + "; ".join(f"{k} — {v}" for k, v in SCOUT_CATEGORIES.items())
        + f". «{SCOUT_REJECT_CATEGORY}» — если история не годится для поста.\n\n"
        "Правила:\n" + _SCOUT_RULES + "\n\n"
        "Одной строкой опиши суть истории (one_liner).\n\n"
        "Ответь строго одним JSON-объектом:\n"
        '{"criteria": {"неожиданность": <0-2>, "наглядность": <0-2>, '
        '"понятность": <0-2>, "релевантность": <0-2>, '
        '"потенциал обсуждения": <0-2>}, "total": <сумма>, '
        '"category": "<из списка>", "one_liner": "<одна строка>"}\n\n'
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
    return _parse_scout_score(response.choices[0].message.content)


_OLD_EPOCH = editorial._OLD_EPOCH


def _topic_sort_key(topic: dict):
    return (topic["total"], topic["article"].get("published") or _OLD_EPOCH)


def select_scout_topics(articles: list[dict], groq_client=None) -> tuple[list[dict], bool]:
    """Score candidates into scout topics. AI mode: keep total >= LIST_TOTAL
    (8-10 приоритет, 6-7 резерв). Fallback mode (no client or failed call):
    keyword totals marked ai=False, top SCOUT_LIST_MAX with total >= FALLBACK_TOTAL."""
    used_ai = bool(groq_client)
    topics = []
    for article in articles:
        full_text = None
        if groq_client:
            full_text = news_pipeline.extract_full_text(article["url"])
            article["_full_text"] = full_text
        result = score_scout_topic_ai(article, groq_client, full_text=full_text)
        if result:
            topic = {"article": article, **result, "ai": True}
        else:
            topic = {"article": article, "criteria": {},
                     "total": score_article(article),
                     "category": "не определена", "one_liner": article["title"],
                     "ai": False}
        topics.append(topic)

    if used_ai:
        topics = [t for t in topics
                  if (t["ai"] and t["total"] >= LIST_TOTAL)
                  or (not t["ai"] and t["total"] >= FALLBACK_TOTAL)]
    else:
        topics = [t for t in topics if t["total"] >= FALLBACK_TOTAL]
    topics.sort(key=_topic_sort_key, reverse=True)
    return topics[:SCOUT_LIST_MAX], used_ai


DATELESS_BADGE = "трендинг на момент сбора, без даты выхода"


def render_scout_list(topics: list[dict], hours: int, used_ai: bool) -> str:
    """Compact numbered topic list for /scout (Telegram HTML)."""
    import html
    lines = [f"🔎 <b>Необычное — подборка скаута</b> ({hours} ч)", ""]
    if not topics:
        lines.append("Ничего необычного не нашлось. Попробуй шире: /scout 168")
        return "\n".join(lines)
    if not used_ai:
        lines.append("⚠️ Режим без AI — оценки приблизительные (по ключевым словам)")
        lines.append("")
    for i, t in enumerate(topics, 1):
        status = "приоритет" if t["total"] >= PRIORITY_TOTAL else "резерв"
        label = SCOUT_CATEGORIES.get(t.get("category"), t.get("category") or "")
        one_liner = t["one_liner"] or t["article"]["title"]
        lines.append(f"<b>{i}. [{t['total']}/10 · {html.escape(label)} · {status}]</b> "
                     f"{html.escape(one_liner)}")
        src_line = f"   ↳ {html.escape(t['article']['source'])} · /scout {i}"
        if t["article"].get("published") is None:
            src_line += f" · {DATELESS_BADGE}"
        lines.append(src_line)
        url = t["article"].get("url") or ""
        if url:
            lines.append(f'   🔗 <a href="{html.escape(url, quote=True)}">'
                         f'{html.escape(url)}</a>')
        lines.append("")
    lines.append("Карточка: /scout 1 … /scout %d" % len(topics))
    return "\n".join(lines)


SCOUT_POST_FORMATS = ("новость с объяснением", "необычный эксперимент",
                      "забавный провал", "практический разбор инструмента",
                      "дискуссионный пост", "короткий видеопост",
                      "опрос аудитории")

MAX_CLAIMS = 6

_CARD_SYSTEM_PROMPT = (
    "Ты — редактор-скаут Telegram-канала Zerocoder (ИИ и вайбкодинг). Пиши только "
    "на русском языке. Ничего не выдумывай: используешь ТОЛЬКО факты из материала "
    "ниже. Происхождение вирусных видео и изображений не проверяемо автоматически — "
    "если материал не подтверждает его, пиши «происхождение не проверено». Заголовок-"
    "крючок передаёт суть, но не преувеличивает. Не пиши готовый Telegram-пост. "
    "Отвечай СТРОГО одним валидным JSON-объектом, без пояснений."
)


def _parse_scout_card_json(raw) -> dict | None:
    """Validate card builder JSON per spec §8 fields. Returns None on failure."""
    data = editorial._extract_json_object(raw)
    if not data:
        return None
    hook = clean_text(str(data.get("hook", ""))).strip()
    what_happened = clean_text(str(data.get("what_happened", ""))).strip()
    fmt = str(data.get("post_format", "")).strip()
    if not hook or not what_happened or fmt not in SCOUT_POST_FORMATS:
        return None
    claims = data.get("claims")
    if not isinstance(claims, list):
        claims = []
    claims = [clean_text(str(c)).strip() for c in claims][:MAX_CLAIMS]
    claims = [c for c in claims if c]
    return {"hook": hook, "what_happened": what_happened,
            "why_interesting": clean_text(str(data.get("why_interesting", ""))).strip(),
            "post_format": fmt,
            "visual": clean_text(str(data.get("visual", ""))).strip() or "не найден",
            "score_why": clean_text(str(data.get("score_why", ""))).strip(),
            "claims": claims}


def _scout_card_stub(topic: dict) -> dict:
    article = topic["article"]
    return {
        "hook": article["title"],
        "category": topic.get("category", "не определена"),
        "total": topic["total"],
        "what_happened": (article.get("summary") or "")[:600],
        "why_interesting": "", "post_format": "",
        "visual": "не найден", "score_why": "",
        "claims": [], "confirmed": [], "needs_check": [], "sources": [],
        "status": "требует проверки",
        "first_source_url": article["url"],
        "no_ai": True,
    }


def build_scout_card(topic: dict, groq_client=None) -> dict:
    """Scout card for one topic (spec §8: 7-template fields + claims). With a
    client: one AI call over the full text; without (or on failure) — stub."""
    card = _scout_card_stub(topic)
    if not groq_client:
        return card
    article = topic["article"]
    content = article.get("_full_text") or article.get("summary", "")
    prompt = (
        "Собери редакторскую карточку скаута по материалу ниже.\n\n"
        f"Категория (выбрана скорингом): {topic['category']}\n\n"
        f"{notifier._NATURAL_TEXT_RULES}\n\n"
        "Ответь строго одним JSON-объектом:\n"
        '{"hook": "<заголовок-крючок>", "what_happened": "<кто, что и когда сделал, '
        '2-4 предложения>", "why_interesting": "<чем выделяется, 1-2 предложения>", '
        '"post_format": "<один из: ' + "; ".join(SCOUT_POST_FORMATS) + '>", '
        '"visual": "<какой есть визуал в материале (видео/фото/скриншот/демо) и ссылка '
        'из текста, если есть; если нет подтверждения происхождения — так и пиши>", '
        '"score_why": "<краткое объяснение оценки 0-10>", '
        '"claims": ["<проверяемое утверждение 1>", "<...>"] — все проверяемые факты '
        'карточки по одному, максимум 6}\n\n'
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
        parsed = _parse_scout_card_json(response.choices[0].message.content)
    except Exception:
        parsed = None
    if not parsed:
        return card
    card.update(parsed)
    card["no_ai"] = False
    return card


def get_or_build_scout_card(topic: dict, groq_client=None) -> dict:
    """Caching wrapper: once a card is built for a topic it is reused —
    /scout N again never burns a second AI call on the same topic. A built
    card goes through fact_check_scout_card (later task) if AI produced it."""
    if topic.get("card") is None:
        card = build_scout_card(topic, groq_client)
        article = topic["article"]
        if not card["no_ai"]:
            card = fact_check_scout_card(card, article, groq_client)
        card["published"] = article.get("published")
        topic["card"] = card
    return topic["card"]


_FC_SYSTEM_PROMPT = (
    "Ты — фактчекер Telegram-канала. Пиши только на русском языке. "
    "Отвечай СТРОГО одним валидным JSON-объектом, без пояснений."
)


def _parse_scout_factcheck(raw) -> dict | None:
    """Parse the card fact-checker output: {verdicts: [...], hook, what_happened}
    where verdicts use the digest fact-check schema (n / verdict / text / query)."""
    data = editorial._extract_json_object(raw)
    if not data or not isinstance(data.get("verdicts"), list):
        return None
    checks = notifier._parse_factcheck_json(_json.dumps(data["verdicts"]))
    return {"verdicts": checks,
            "hook": str(data.get("hook", "")).strip(),
            "what_happened": str(data.get("what_happened", "")).strip()}


def _verification_status(card: dict) -> str:
    """подтверждено — все claims ok; частично подтверждено — есть
    подтверждённые и хотя бы один uncertain с корроборацией; иначе
    (что-то не сверено / uncertain без корроборации / claim убит как wrong)
    — требует проверки."""
    claims = card.get("claims", [])
    confirmed = card.get("confirmed", [])
    needs = card.get("needs_check", [])
    if not claims:
        return "требует проверки"
    if not needs and len(confirmed) == len(claims):
        return "подтверждено"
    if confirmed and any(n.startswith("✓ (подтверждено другими СМИ)") for n in needs):
        return "частично подтверждено"
    return "требует проверки"


def fact_check_scout_card(card: dict, article: dict, groq_client=None) -> dict:
    """Fact-check the card's numbered claims against the article text
    (digest verdict scheme: ok/uncertain/wrong), then corroboration of the
    uncertain ones via Google News (keyless). wrong claims leave both lists
    and the checker rewrites what_happened from the confirmed facts only.
    Without a client: claims land in needs_check unverified. Always sets
    card["status"]. Never raises."""
    claims = card.get("claims", [])
    if not groq_client or not claims:
        card["confirmed"] = []
        card["needs_check"] = list(claims)
        card["status"] = _verification_status(card)
        return card

    full_text = article.get("_full_text") or article.get("summary", "")
    numbered = "\n".join(f"{i + 1}. {claim}" for i, claim in enumerate(claims))
    prompt = (
        "Сверь каждое утверждение карточки с текстом статьи:\n"
        "- «ok» — подтверждается текстом статьи.\n"
        "- «uncertain» — не подтверждается, но и не противоречит.\n"
        "- «wrong» — противоречит тексту статьи.\n\n"
        "В «what_happened» верни поле, где wrong-утверждения удалены, а "
        "uncertain переформулированы только из подтверждённого. Пустая строка — "
        "если правок нет.\n\n"
        "Для каждого утверждения — query: короткий поисковый запрос 2-4 слова "
        "по ключевым сущностям, чтобы проверить, о том же ли пишут другие СМИ.\n\n"
        "Ответь строго одним JSON-объектом:\n"
        '{"verdicts": [{"n": <номер>, "verdict": "ok|uncertain|wrong", '
        '"text": "<верифицированная формулировка>", "query": "<запрос>"}], '
        '"hook": "<исправленный заголовок-крючок или пусто>", '
        '"what_happened": "<исправленное поле или пусто>"}\n\n'
        f"Текст статьи: {full_text[:1800]}\n"
        f"(оригинал: {article.get('url', '')})\n\n"
        "Утверждения карточки:\n" + numbered
    )
    try:
        response = groq_client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": _FC_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            max_tokens=2000,
            reasoning_effort="low",
        )
        data = _parse_scout_factcheck(response.choices[0].message.content)
    except Exception:
        data = None

    if not data or not data["verdicts"]:
        card["confirmed"] = []
        card["needs_check"] = list(claims)
        card["status"] = _verification_status(card)
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
        # uncertain — веб-корроборация через Google News
        try:
            found = news_pipeline.fetch_google_news([check["query"]], hours=24) \
                if check["query"] else []
        except Exception:
            found = []
        corroborated = len(notifier._publishers_from_google_news(found)) >= 2
        for a in found:
            key = news_pipeline.normalize_url(a["url"])
            if key != own_key and key not in seen:
                seen.add(key)
                sources.append({"title": a["title"], "url": a["url"]})
        label = "✓ (подтверждено другими СМИ): " if corroborated else ""
        needs_check.append(label + text)

    card["confirmed"] = confirmed
    card["needs_check"] = needs_check
    card["sources"] = sources[:3]
    if data["hook"]:
        card["hook"] = clean_text(data["hook"])
    if data["what_happened"]:
        card["what_happened"] = clean_text(data["what_happened"])
    card["status"] = _verification_status(card)
    return card


def render_scout_card_messages(topic: dict, card: dict) -> list[str]:
    """Scout card as Telegram HTML messages per the §7 template order.
    Everything html-escaped, ≤ 4096 each (editorial._split_by_paragraphs)."""
    import html
    label = SCOUT_CATEGORIES.get(topic.get("category"), topic.get("category") or "—")
    facts = "\n".join(f"• {html.escape(f)}" for f in card.get("confirmed", [])) or "—"
    needs = "\n".join(f"• {html.escape(n)}" for n in card.get("needs_check", [])) or "—"
    sources = "\n".join(
        f'<a href="{html.escape(s["url"], quote=True)}">{html.escape(s["title"])}</a>'
        for s in card.get("sources", [])[:3]) or "—"
    text = "\n".join(filter(None, [
        f"🗂 <b>{html.escape(card['hook'])}</b>",
        f"Категория: {html.escape(label)} · Оценка: {card.get('total', 0)}/10 · "
        f"Статус проверки: {html.escape(card.get('status', 'требует проверки'))}",
        "Дата публикации: " + editorial._fmt_date(card.get("published")),
        "",
        "Что произошло:",
        html.escape(card["what_happened"]),
        f"Почему интересно: {html.escape(card.get('why_interesting', ''))}" if card.get("why_interesting") else "",
        f"Потенциал поста: {html.escape(card.get('post_format', ''))}" if card.get("post_format") else "",
        f"Визуал: {html.escape(card.get('visual', ''))}" if card.get("visual") else "",
        "Источники:",
        f'🔗 <a href="{html.escape(card.get("first_source_url", ""), quote=True)}">'
        f'{html.escape(card.get("first_source_url", ""))}</a>',
        "Доп. источники: " + sources,
        "Подтверждённые факты:\n" + facts,
        "Что требует проверки:\n" + needs,
        f"Оценка: {html.escape(card.get('score_why', ''))}" if card.get("score_why") else "",
    ]))
    return editorial._split_by_paragraphs(text)


def resolve_scout_command(args: list[str], n_topics: int) -> dict:
    """Parse /scout arguments: a number 1..n_topics with a non-empty stored
    list means card N; anything else (no arg, garbage, empty list) means a
    time window in hours, clamped to 2..168."""
    n = editorial.parse_int_arg(list(args or []))
    if n is None:
        return {"mode": "hours", "value": DEFAULT_HOURS}
    if 1 <= n <= n_topics:
        return {"mode": "card", "n": n}
    return {"mode": "hours", "value": min(max(n, 2), 168)}