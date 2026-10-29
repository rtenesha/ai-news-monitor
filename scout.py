#!/usr/bin/env python3
"""AI-scout digest per the 2026-10-29 editor instruction: unusual, fresh,
potentially viral AI stories — collection, 5-criteria scoring (max 10),
editor cards with fact-check. Spec:
docs/superpowers/specs/2026-10-29-scout-design.md."""

from __future__ import annotations

import re
import urllib.request

import editorial
import news_pipeline
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
    {"name": "arXiv cs.RO", "url": "https://export.arxiv.org/rss/cs.RO"},
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
    """Front-page papers of huggingface.co/papers (no official RSS).
    Never raises — a failed or unparsable page just yields no candidates."""
    url = "https://huggingface.co/papers"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": news_pipeline._BROWSER_UA})
        with urllib.request.urlopen(req, timeout=15) as resp:
            html_text = resp.read().decode("utf-8", "replace")
    except Exception:
        return []
    papers, seen = [], set()
    for pid, title in re.findall(
            r'href="/papers/(\d{4}\.\d{4,6})"[^>]*>([^<]+)<', html_text):
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
        posts, _ok = news_pipeline.fetch_rss_feed(feed["name"], feed["url"], hours)
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
    one_liner = clean_text(str(data.get("one_liner", ""))).strip()
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