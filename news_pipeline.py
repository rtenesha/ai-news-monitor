#!/usr/bin/env python3
"""Shared pipeline helpers for monitor.py, notifier.py, and bot.py: extra
keyless sources, full-text extraction, structured AI scoring, and
deduplication. See docs/superpowers/specs/2026-07-22-news-pipeline-design.md."""

import json
import re
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlsplit, parse_qsl, urlencode, urlunsplit

from pydantic import BaseModel, Field, ValidationError
from tenacity import retry, stop_after_attempt, wait_exponential

import feedparser
import trafilatura

_TRACKING_QUERY_PARAMS = {
    "fbclid", "gclid", "dclid", "igshid", "mc_cid", "mc_eid", "msclkid",
    "ttclid", "twclid", "vero_id", "li_fat_id", "_ga",
}


def normalize_url(url: str) -> str:
    """Normalized identity key for cross-source dedup: lowercase host,
    no 'www.', no trailing slash, no tracking query params, scheme ignored."""
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = parsed.path.rstrip("/") or "/"
    kept_params = sorted(
        (k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_QUERY_PARAMS
    )
    query = urlencode(kept_params)
    return urlunsplit(("https", host, path, query, ""))


def dedup_cross_source(articles: list[dict]) -> list[dict]:
    """Merge articles that point at the same normalized URL. Keeps the
    richer (longer) summary as primary and combines distinct source names
    with ' + '."""
    groups: dict[str, list[dict]] = {}
    for a in articles:
        groups.setdefault(normalize_url(a["url"]), []).append(a)

    merged = []
    for group in groups.values():
        if len(group) == 1:
            merged.append(group[0])
            continue
        primary = dict(max(group, key=lambda a: len(a.get("summary", ""))))
        other_sources = [
            a["source"] for a in group
            if a["source"] != primary["source"]
        ]
        if other_sources:
            names = list(dict.fromkeys([primary["source"], *other_sources]))
            primary["source"] = " + ".join(sorted(names))
        merged.append(primary)
    return merged


class AnalysisResult(BaseModel):
    score: int = Field(ge=0, le=5)
    reason: str
    summary: str


def _parse_analysis_json(raw: str) -> Optional[AnalysisResult]:
    """Extract and validate a {score, reason, summary} JSON object from a
    raw model response, tolerating ```json fences or surrounding prose."""
    if not isinstance(raw, str):
        return None
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    try:
        return AnalysisResult.model_validate(data)
    except ValidationError:
        return None


_ANALYSIS_SYSTEM_PROMPT = (
    "Ты пишешь исключительно на русском языке. Никаких китайских, японских "
    "или других иероглифов — только кириллица, латиница в именах собственных и цифры. "
    "Отвечай СТРОГО одним JSON-объектом, без пояснений и markdown-разметки."
)


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
def _call_analysis_model(client, article: dict, full_text: Optional[str]) -> str:
    content = full_text or article["summary"]
    prompt = (
        "Ты — опытный русскоязычный редактор Telegram-канала об ИИ и вайбкодинге.\n\n"
        "Оцени статью по релевантности для аудитории, которой интересны:\n"
        "— новые ИИ-инструменты и нейросети\n"
        "— вайбкодинг и автоматизация с помощью ИИ\n"
        "— практические кейсы применения ИИ, скилы, промты, MCP, лайфхаки\n\n"
        "Ответь строго одним JSON-объектом вида:\n"
        '{"score": <целое 0-5>, "reason": "<почему такая оценка, коротко>", '
        '"summary": "<одно законченное предложение на живом русском языке — суть новости>"}\n\n'
        f"Источник: {article['source']}\n"
        f"Заголовок: {article['title']}\n"
        f"Содержание: {content[:3000]}"
    )
    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[
            {"role": "system", "content": _ANALYSIS_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        max_tokens=400,
    )
    return response.choices[0].message.content


def score_article_ai(article: dict, groq_client, full_text: Optional[str] = None) -> Optional[AnalysisResult]:
    """Score one article with structured AI output (0-5 + reason + one-line
    summary). Returns None if the call or parsing failed after retries —
    callers must fall back to keyword scoring."""
    try:
        raw = _call_analysis_model(groq_client, article, full_text)
    except Exception:
        return None
    return _parse_analysis_json(raw)


_BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"


def _extract_from_html(html: str) -> Optional[str]:
    try:
        text = trafilatura.extract(html, favor_recall=True)
    except Exception:
        return None
    if not text or len(text) < 200:
        return None
    return text


def extract_full_text(url: str) -> Optional[str]:
    """Fetch `url` and extract the article body via trafilatura. Returns
    None on any failure (network error, blocked page, too-short
    extraction) — callers must fall back to the RSS summary."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _BROWSER_UA})
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
    except Exception:
        return None
    return _extract_from_html(html)


def _parse_duplicate_groups(raw: str, n_items: int) -> list[list[int]]:
    """Extract and validate duplicate groups from AI response JSON.
    Returns list of valid groups (each with >= 2 valid indices), or empty list
    if parsing fails or no valid groups found."""
    if not isinstance(raw, str):
        return []
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    groups = data.get("duplicates", [])
    if not isinstance(groups, list):
        return []
    valid = []
    for group in groups:
        if not isinstance(group, list) or len(group) < 2:
            continue
        if all(isinstance(i, int) and 0 <= i < n_items for i in group):
            valid.append(group)
    return valid


def _apply_duplicate_groups(articles: list[dict], groups: list[list[int]]) -> list[dict]:
    """Remove lower-scored duplicates from each group, keeping the highest-scored article."""
    drop: set[int] = set()
    for group in groups:
        ranked = sorted(group, key=lambda i: articles[i].get("score", 0), reverse=True)
        drop.update(ranked[1:])
    return [a for i, a in enumerate(articles) if i not in drop]


def dedup_semantic(articles: list[dict], groq_client) -> list[dict]:
    """Collapse near-duplicate stories (same event, different sources)
    using one batched AI call over already-scored articles. Falls back to
    `articles` unchanged if the call or parsing fails."""
    if len(articles) <= 1:
        return articles

    try:
        numbered = "\n\n".join(
            f"[{i}] {a['title']}\n{a.get('verdict') or a.get('summary', '')[:200]}"
            for i, a in enumerate(articles)
        )
        prompt = (
            "Ниже пронумерованный список новостей. Найди группы, где несколько "
            "пунктов рассказывают об одном и том же событии (просто с разных сайтов). "
            "Ответь строго одним JSON-объектом:\n"
            '{"duplicates": [[i, j, ...], ...]}\n'
            "Каждая группа — индексы (начиная с 0) новостей об одном и том же событии, "
            'минимум 2 индекса. Если дублей нет — верни {"duplicates": []}.\n\n'
            f"{numbered}"
        )
        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": "Отвечай СТРОГО одним JSON-объектом, без пояснений."},
                {"role": "user", "content": prompt},
            ],
            max_tokens=500,
        )
        raw = response.choices[0].message.content

        groups = _parse_duplicate_groups(raw, len(articles))
        if not groups:
            return articles
        return _apply_duplicate_groups(articles, groups)
    except Exception:
        return articles


def _safe_int(value) -> int:
    """Safely convert a value to int, returning 0 on any failure."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _hn_story_to_article(story: dict) -> Optional[dict]:
    """Convert a Hacker News API story object to article dict. Returns None if
    title is missing."""
    title = story.get("title")
    if not title:
        return None
    story_id = story["id"]
    url = story.get("url") or f"https://news.ycombinator.com/item?id={story_id}"
    return {
        "title": title,
        "url": url,
        "summary": (story.get("text") or "")[:600],
        "source": "Hacker News",
        "published": None,
    }


def fetch_hackernews(hours: int = 24, min_score: int = 100, fetch_top: int = 30) -> list[dict]:
    """Top Hacker News stories via the keyless Firebase API, filtered by
    minimum score and publish time. Returns [] on any failure."""
    base = "https://hacker-news.firebaseio.com/v0"
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    try:
        with urllib.request.urlopen(f"{base}/topstories.json", timeout=10) as resp:
            story_ids = json.loads(resp.read())[:fetch_top]
    except Exception:
        return []

    articles = []
    for story_id in story_ids:
        try:
            with urllib.request.urlopen(f"{base}/item/{story_id}.json", timeout=10) as resp:
                story = json.loads(resp.read())
            if not story or story.get("score", 0) < min_score:
                continue
            story_time = story.get("time")
            if story_time and datetime.fromtimestamp(story_time, tz=timezone.utc) < cutoff:
                continue
            article = _hn_story_to_article(story)
            if article:
                articles.append(article)
        except Exception:
            continue
    return articles


def _github_trending_period(hours: int) -> str:
    """Map hours window to GitHub/OSS Insight trending period."""
    return "past_24_hours" if hours <= 24 else "past_28_days"


def fetch_github_trending(hours: int = 24, languages: Optional[list] = None, min_stars: int = 5) -> list[dict]:
    """Trending GitHub repos via the keyless OSS Insight API. Returns []
    entries for languages that fail; never raises."""
    languages = languages or ["Python", "TypeScript", "All"]
    period = _github_trending_period(hours)
    articles = []
    for lang in languages:
        params = urllib.parse.urlencode({"period": period, "language": lang})
        req = urllib.request.Request(
            f"https://api.ossinsight.io/v1/trends/repos?{params}",
            headers={"Accept": "application/json", "User-Agent": "Zerocoder-News-Bot/1.0"},
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                payload = json.loads(resp.read())
            rows = (payload.get("data") or {}).get("rows") or []
        except Exception:
            continue
        for row in rows:
            try:
                repo = row.get("repo_name")
                stars = _safe_int(row.get("stars"))
                if not repo or stars < min_stars:
                    continue
                description = (row.get("description") or "").strip()
                articles.append({
                    "title": f"{repo} (+{stars}⭐)",
                    "url": f"https://github.com/{repo}",
                    "summary": description[:600],
                    "source": "GitHub Trending",
                    "published": None,
                })
            except Exception:
                continue
    return articles


def _google_news_time_operator(hours: int) -> str:
    """Generate Google News search time operator based on hours window."""
    if hours <= 100:
        return f"when:{hours}h"
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    return f"after:{since.strftime('%Y-%m-%d')}"


def fetch_google_news(queries: list[str], hours: int = 24) -> list[dict]:
    """Google News RSS search for each query in `queries`. Keyless. Skips
    queries that fail; never raises."""
    operator = _google_news_time_operator(hours)
    articles = []
    for query in queries:
        params = urllib.parse.urlencode({
            "q": f"{query} {operator}",
            "hl": "ru", "gl": "RU", "ceid": "RU:ru",
        })
        try:
            feed = feedparser.parse(f"https://news.google.com/rss/search?{params}")
        except Exception:
            continue
        for entry in feed.entries:
            title = entry.get("title", "")
            link = entry.get("link", "")
            if not title or not link:
                continue
            summary = entry.get("summary", entry.get("description", ""))
            summary = re.sub(r"<[^>]+>", " ", summary)
            articles.append({
                "title": title,
                "url": link,
                "summary": re.sub(r"\s+", " ", summary).strip()[:600],
                "source": f"Google News: {query}",
                "published": None,
            })
    return articles
