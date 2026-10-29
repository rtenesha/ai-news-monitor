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