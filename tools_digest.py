"""Tools digest pipeline: collect → check official pages → one AI selection."""
from __future__ import annotations

import json as _json
import re
import urllib.request
from typing import Optional

from notifier import KEYWORDS, _kw_matches, score_article
import news_pipeline

_BROWSER_UA = news_pipeline._BROWSER_UA

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
    if m and "/tree/" not in url and not url.endswith(".md"):
        user, repo = m.group(1), m.group(2)
        info["readme"] = _gh_get(
            f"https://raw.githubusercontent.com/{user}/{repo}/main/README.md", "text")
        rel = _gh_json(f"https://api.github.com/repos/{user}/{repo}/releases/latest")
        if rel:
            info["last_release"] = rel.get("tag_name")
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
            status = resp.status
        if status >= 400:
            return None
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