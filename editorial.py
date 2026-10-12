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