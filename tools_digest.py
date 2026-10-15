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