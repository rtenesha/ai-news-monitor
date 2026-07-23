#!/usr/bin/env python3
"""Shared pipeline helpers for monitor.py, notifier.py, and bot.py: extra
keyless sources, full-text extraction, structured AI scoring, and
deduplication. See docs/superpowers/specs/2026-07-22-news-pipeline-design.md."""

import json
import re
import urllib.request
from typing import Optional
from urllib.parse import urlsplit, parse_qsl, urlencode, urlunsplit

from pydantic import BaseModel, Field, ValidationError
from tenacity import retry, stop_after_attempt, wait_exponential

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
    req = urllib.request.Request(url, headers={"User-Agent": _BROWSER_UA})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
    except Exception:
        return None
    return _extract_from_html(html)
