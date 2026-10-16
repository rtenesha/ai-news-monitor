"""Pain points pipeline: discussions → comments → fragments → one AI call → cards."""
from __future__ import annotations

import html
import json as _json
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

import news_pipeline
from notifier import KEYWORDS, HIGH_VALUE, _kw_matches
from tools_digest import _stem_matches

_BROWSER_UA = news_pipeline._BROWSER_UA
HN_BASE = "https://hacker-news.firebaseio.com/v0"

_DEFAULT_HOURS = 48
PAIN_DISCUSSION_CAP = 16
REDDIT_DISCUSSION_CAP = 8
HN_DISCUSSION_CAP = 8

HNS_TOP_COUNT = 60
HNS_ASK_COUNT = 30

_REDDIT_JSON_DISABLED = False

time_sleep = time.sleep  # module-level для патчей в тестах


# --- HN: discussion collectors -------------------------------------------

def _hn_get_json(path: str, timeout: int = 12) -> Optional[Any]:
    """Firebase item/list fetch with one retry. Never raises."""
    url = f"{HN_BASE}/{path}"
    for i in range(2):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                return _json.loads(resp.read())
        except Exception:
            if i == 0:
                time.sleep(1)
    return None


def _window_ok(story: dict, cutoff: float) -> bool:
    t = story.get("time") or 0
    return t >= cutoff


def _story_to_discussion(story: dict, source: str) -> dict:
    sid = story.get("id")
    item_url = f"https://news.ycombinator.com/item?id={sid}"
    return {
        "title": story.get("title", ""),
        "url": story.get("url") or item_url,
        "summary": (story.get("text") or "").replace("<p>", " ").strip()[:600],
        "source": source or "Hacker News",
        "published": None,
        "score": story.get("score", 0),
        "num_comments": None,
        "hn_id": sid,
        "reddit_fullname": None,
        "_story": story,
        "_permalink": None,
    }


def _fetch_hn_discussions(hours: int) -> list[dict]:
    cutoff = time.time() - hours * 3600
    top_ids = _hn_get_json("topstories.json") or []
    ask_ids = _hn_get_json("askstories.json") or []
    all_ids = top_ids[:HNS_TOP_COUNT] + ask_ids[:HNS_ASK_COUNT]
    seen: set = set()
    stories = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        for story in pool.map(_hn_get_json,
                              [f"item/{i}.json" for i in all_ids]):
            if not story or story.get("id") in seen:
                continue
            seen.add(story.get("id"))
            min_score = (100 if story["id"] in set(top_ids[:HNS_TOP_COUNT]) else 50)
            if story.get("score", 0) < min_score or not _window_ok(story, cutoff):
                continue
            stories.append(story)
    return [_story_to_discussion(story, "Hacker News") for story in stories]


def _pain_score(article: dict) -> int:
    """Keyword-балл релевантности: стем-матч по KEYWORDS, HIGH_VALUE дороже."""
    text = (article["title"] + " " + article["summary"]).lower()
    score = 0
    for kw in KEYWORDS:
        if _stem_matches(kw, text):
            score += 2 if kw.lower() in HIGH_VALUE else 1
    return score


# --- Reddit: JSON-based discussion collector ------------------------------

def _reddit_new_json(sub: str, hours: int) -> Optional[dict]:
    url = f"https://www.reddit.com/r/{sub}/new.json?limit=50"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _BROWSER_UA})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return _json.loads(resp.read())
    except Exception:
        return None


def _fetch_reddit_discussions(hours: int) -> list[dict]:
    """Per-subreddit /new.json; отбор по score/num_comments; флаг-деградация."""
    global _REDDIT_JSON_DISABLED
    if _REDDIT_JSON_DISABLED:
        return []
    cutoff = time.time() - hours * 3600
    discussions = []
    subs = news_pipeline.REDDIT_SUBREDDITS
    for i, sub in enumerate(subs):
        if i:
            time_sleep(2)  # Reddit 429s on burst requests
        data = _reddit_new_json(sub, hours)
        if data is None:
            continue
        for child in data.get("data", {}).get("children", []):
            post = child.get("data", {})
            if post.get("score", 0) < 20 and post.get("num_comments", 0) < 10:
                continue
            if (post.get("created_utc") or 0) < cutoff:
                continue
            permalink = post.get("permalink")
            if not permalink or not post.get("title"):
                continue
            discussions.append({
                "title": post["title"],
                "url": "https://www.reddit.com" + permalink,
                "summary": (post.get("selftext") or "")[:600],
                "source": f"Reddit: r/{sub}",
                "published": None,
                "score": post.get("score", 0),
                "num_comments": post.get("num_comments"),
                "hn_id": None,
                "reddit_fullname": post.get("name"),
                "_story": None,
                "_permalink": permalink,
            })
    return discussions


# --- Merge ----------------------------------------------------------------

def collect_discussions(hours: int = _DEFAULT_HOURS) -> list[dict]:
    """Merge HN discussions and Reddit posts, dedup, keyword-filter, cap.
    Never raises (each fetcher is individually safe)."""
    discussions = _fetch_hn_discussions(hours) + _fetch_reddit_discussions(hours)
    discussions = news_pipeline.dedup_cross_source(discussions)
    relevant = [d for d in discussions if _pain_score(d) > 0]
    relevant.sort(key=_pain_score, reverse=True)
    hn = [d for d in relevant if d["hn_id"] is not None][:HN_DISCUSSION_CAP]
    reddit = [d for d in relevant if d["reddit_fullname"] is not None][:REDDIT_DISCUSSION_CAP]
    return hn + reddit