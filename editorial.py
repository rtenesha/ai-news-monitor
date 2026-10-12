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