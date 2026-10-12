#!/usr/bin/env python3
"""Editorial cards per the 2026-10-12 editor instruction: topic collection
from keyless sources, 6-criteria AI scoring (max 12), editor cards with
draft + fact-check. Spec: docs/superpowers/specs/2026-10-12-editorial-cards-design.md."""

from __future__ import annotations

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
    Рубрика «Инфоповод-пустышка» = тема не годится → None (отброс)."""
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


_OLD_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _topic_sort_key(topic: dict):
    return (topic["total"], topic["article"].get("published") or _OLD_EPOCH)


def select_topics(articles: list[dict], groq_client=None) -> tuple[list[dict], bool]:
    """Score candidates into editor topics. AI mode: keep total >= LIST_TOTAL
    (9-12 приоритет, 6-8 резерв). Fallback mode (no client or failed calls):
    keyword totals marked ai=False, top TOPIC_LIST_MAX with total >= FALLBACK_TOTAL."""
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
        # AI-темы по порогу резерва; статьи, у которых AI не прошёл (ai=False),
        # остаются по keyword-порогу — иначе per-article fallback был бы мёртвым
        # кодом: keyword total всегда ≤ 5 и каждая такая тема отбрасывалась бы.
        topics = [t for t in topics
                  if (t["ai"] and t["total"] >= LIST_TOTAL)
                  or (not t["ai"] and t["total"] >= FALLBACK_TOTAL)]
    else:
        topics = [t for t in topics if t["total"] >= FALLBACK_TOTAL]
    topics.sort(key=_topic_sort_key, reverse=True)
    return topics[:TOPIC_LIST_MAX], used_ai


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
        '"claims": ["<утверждение 1>", "<утверждение 2>"] — все проверяемые факты '
        'черновика по одному в строке, максимум 6}\n\n'
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